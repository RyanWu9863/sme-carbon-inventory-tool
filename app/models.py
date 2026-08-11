"""
資料模型 — 對齊環境部「溫室氣體排放量清冊表單」

對照關係（口試時你要能直接講出這張對照表）：
    表一/表二 → Organization、BoundaryExclusion
    表三      → EmissionSource
    表四      → ActivityRecord
    表五      → EmissionResult（計算結果與係數溯源）
    表六      → ActivityRecord 的品質欄位
    附表一    → PublishedFactor、ElectricityFactor
    附表二    → GwpValue
    附表五~七 → ProcessCode、EquipmentCode、MaterialCode

三個核心設計決策：

1. 係數拆成兩層。
   官方公告的是「每單位熱值」的係數(kg/TJ)，要乘上燃料熱值才得到「每公升」。
   熱值是假設值、會改版、使用者可覆寫；公告係數則是固定值。
   混在一起存，就無法回答「這個數字是用哪個熱值算的」。

2. 電力走完全不同的路徑。
   能源署公告的電力排碳係數已是合併 CO2e，不可再乘 GWP。
   因此獨立成 ElectricityFactor，而不是塞進 PublishedFactor。

3. 計算結果做快照。
   把當下用的公告係數、熱值、GWP 版本全部釘住。
   來源改版後，歷史報告數字仍可重現。
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base


# --------------------------------------------------------------------------
# 列舉
# --------------------------------------------------------------------------

class DirectIndirect(str, enum.Enum):
    DIRECT = "直接"
    INDIRECT = "間接"


class EmissionType(str, enum.Enum):
    """表三「排放型式」欄位的官方選項"""
    STATIONARY = "固定燃燒"
    MOBILE = "移動燃燒"
    PROCESS = "製程"
    FUGITIVE = "逸散"
    PURCHASED_ELECTRICITY = "外購電力"
    PURCHASED_STEAM = "外購蒸汽"


class FuelUsage(str, enum.Enum):
    """附表一分固定燃燒與移動燃燒兩張表，同一燃料係數不同"""
    STATIONARY = "stationary"
    MOBILE = "mobile"


class DataQuality(str, enum.Enum):
    MEASURED = "實測"
    ESTIMATED = "推估"


class EvidenceType(str, enum.Enum):
    MANUAL = "手動輸入"
    UTILITY_BILL = "電費單"
    GAS_BILL = "瓦斯帳單"
    EINVOICE = "電子發票"
    CARRIER_CSV = "載具CSV"
    RECEIPT_OCR = "紙本發票OCR"


class RecordStatus(str, enum.Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


# --------------------------------------------------------------------------
# 官方代碼表（附表五～七，唯讀參考資料）
# --------------------------------------------------------------------------

class ProcessCode(Base):
    """附表五 製程分類（官方 1,024 筆，匯入時全量帶入）"""
    __tablename__ = "process_code"
    code = Column(String(12), primary_key=True)
    name = Column(String(120), nullable=False)


class EquipmentCode(Base):
    """附表六 設備分類（官方 359 筆）"""
    __tablename__ = "equipment_code"
    code = Column(String(12), primary_key=True)
    name = Column(String(120), nullable=False)


class MaterialCode(Base):
    """附表七 原(燃)物料或產品分類（官方 6,256 筆）"""
    __tablename__ = "material_code"
    code = Column(String(12), primary_key=True)
    name = Column(String(160), nullable=False)


# --------------------------------------------------------------------------
# 表一 / 表二：事業基本資料與邊界設定
# --------------------------------------------------------------------------

class Organization(Base):
    __tablename__ = "organization"

    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    control_no = Column(String(40))              # 管制編號
    tax_id = Column(String(20))                  # 統一編號
    industry_code = Column(String(12))           # 行業別代碼（附表四）

    reporting_year_roc = Column(Integer, nullable=False)   # 盤查年度（民國）
    base_year_roc = Column(Integer)

    # 表二：營運控制權／財務控制權／股權比例
    boundary_method = Column(String(40), default="營運控制權")
    boundary_method_reason = Column(Text)

    county = Column(String(40))
    district = Column(String(40))
    postal_code = Column(String(10))
    address = Column(String(255))

    created_at = Column(DateTime, default=dt.datetime.utcnow)

    exclusions = relationship("BoundaryExclusion", back_populates="organization")
    sources = relationship("EmissionSource", back_populates="organization")

    @property
    def year_start(self) -> dt.date:
        return dt.date(self.reporting_year_roc + 1911, 1, 1)

    @property
    def year_end(self) -> dt.date:
        return dt.date(self.reporting_year_roc + 1911, 12, 31)


class BoundaryExclusion(Base):
    """
    表二「邊界內未納入計算之排放源」。

    獨立成表而不是備註欄，因為它是口試與查驗的核心證據：
    證明你不是「漏掉」，而是「判斷後排除並記錄理由」。
    reason 為 nullable=False，程式端不接受空值。
    """
    __tablename__ = "boundary_exclusion"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organization.id"), nullable=False)
    excluded_item = Column(String(160), nullable=False)
    reason = Column(Text, nullable=False)
    decided_by = Column(String(80))
    decided_at = Column(Date)

    organization = relationship("Organization", back_populates="exclusions")


# --------------------------------------------------------------------------
# 表三：排放源鑑別（盤查清冊）
# --------------------------------------------------------------------------

class EmissionSource(Base):
    __tablename__ = "emission_source"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organization.id"), nullable=False)
    source_no = Column(String(20), nullable=False)      # S01
    name = Column(String(160), nullable=False)          # 自訂，供人辨識

    # 官方欄位
    process_no = Column(String(20))
    process_code = Column(String(12), ForeignKey("process_code.code"))
    equipment_no = Column(String(20))
    equipment_code = Column(String(12), ForeignKey("equipment_code.code"))
    material_code = Column(String(12), ForeignKey("material_code.code"))
    is_biomass = Column(Boolean, default=False)
    direct_indirect = Column(Enum(DirectIndirect), nullable=False)
    emission_type = Column(Enum(EmissionType), nullable=False)
    emission_subtype = Column(String(80))               # 逸散／外購電力細分類

    produces_co2 = Column(Boolean, default=False)
    produces_ch4 = Column(Boolean, default=False)
    produces_n2o = Column(Boolean, default=False)
    produces_hfcs = Column(Boolean, default=False)
    produces_pfcs = Column(Boolean, default=False)
    produces_sf6 = Column(Boolean, default=False)
    produces_nf3 = Column(Boolean, default=False)
    is_cogeneration = Column(Boolean, default=False)

    # 本系統擴充：官方表三無此欄，但附表一移動燃燒的 CH4/N2O 依此而異
    vehicle_tech = Column(String(60))

    factor_key = Column(String(40))     # 對應到哪組係數
    active = Column(Boolean, default=True)
    note = Column(Text)

    __table_args__ = (UniqueConstraint("org_id", "source_no"),)
    organization = relationship("Organization", back_populates="sources")
    records = relationship("ActivityRecord", back_populates="source")


# --------------------------------------------------------------------------
# 係數：兩層結構
# --------------------------------------------------------------------------

class FactorSet(Base):
    """一份公告的特定版本。改版＝新增一筆，永不 UPDATE 舊的。"""
    __tablename__ = "factor_set"

    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    version = Column(String(60), nullable=False)
    doc_no = Column(String(80))          # 環部授氣字第1139101231號
    publish_date = Column(Date)
    source_url = Column(String(255))
    gwp_standard = Column(String(20), default="AR5")
    note = Column(Text)

    __table_args__ = (UniqueConstraint("name", "version"),)
    published_factors = relationship("PublishedFactor", back_populates="factor_set")


class PublishedFactor(Base):
    """
    附表一：燃料單位熱值之排放係數，單位 kg/TJ。

    這不是「每公升」係數 —— 官方沒有公告每公升值。
    要得到每公升必須乘上熱值，見 HeatingValue。
    """
    __tablename__ = "published_factor"

    id = Column(Integer, primary_key=True)
    factor_set_id = Column(Integer, ForeignKey("factor_set.id"), nullable=False)
    factor_key = Column(String(40), nullable=False, index=True)
    material_code = Column(String(12), ForeignKey("material_code.code"))
    display_name = Column(String(120), nullable=False)

    usage = Column(Enum(FuelUsage), nullable=False)
    vehicle_tech = Column(String(60))         # 移動燃燒才有；固定燃燒為 None

    co2_kg_per_tj = Column(Float, default=0.0)
    ch4_kg_per_tj = Column(Float, default=0.0)
    n2o_kg_per_tj = Column(Float, default=0.0)

    # 查 GwpValue 用的氣體名稱。
    # 依環境部平台公告「修正甲烷及石化甲烷GWP適用情形」，
    # 燃料燃燒（含固定及移動設備）一律採「甲烷」(28)，非「石化甲烷」(30)。
    ch4_gwp_gas = Column(String(20), default="甲烷")

    source_ref = Column(String(120))          # 附表一-固定 第11列
    keywords = Column(Text)
    note = Column(Text)

    factor_set = relationship("FactorSet", back_populates="published_factors")


class HeatingValue(Base):
    """
    燃料熱值（低位／淨熱值）。

    這是系統中唯一「使用者可以合法覆寫官方值」的資料，
    因為環境部原意就是由事業填入自身燃料實際熱值。
    org_id 為 None 代表系統預設（能源署公告），
    有 org_id 代表該事業自行提供。查詢時優先取事業值。
    """
    __tablename__ = "heating_value"

    id = Column(Integer, primary_key=True)
    material_code = Column(String(12), ForeignKey("material_code.code"))
    factor_key = Column(String(40), index=True)
    display_name = Column(String(120), nullable=False)

    unit = Column(String(20), nullable=False)        # 公升 / 立方公尺 / 公斤
    kcal_per_unit = Column(Float, nullable=False)

    source = Column(String(200), nullable=False)
    version = Column(String(60))
    applicable_from_roc = Column(Integer)

    org_id = Column(Integer, ForeignKey("organization.id"))   # None＝系統預設
    is_user_override = Column(Boolean, default=False)
    note = Column(Text)


class ElectricityFactor(Base):
    """
    能源署電力排碳係數（附表一亦收錄）。

    已是合併 CO2e，計算時不可再乘 GWP —— 這是最常見的錯誤。
    逐年公告，盤查年度必須對應。
    """
    __tablename__ = "electricity_factor"

    id = Column(Integer, primary_key=True)
    factor_key = Column(String(40), default="electricity_tw")
    year_roc = Column(Integer, nullable=False, unique=True)
    kgco2e_per_kwh = Column(Float, nullable=False)
    material_code = Column(String(12), default="GG3500")
    source = Column(String(200))
    uncertainty_pct = Column(Float)          # 附表一列 ±7%
    note = Column(Text)


class GwpValue(Base):
    """附表二／附表四 溫暖化潛勢"""
    __tablename__ = "gwp_value"

    id = Column(Integer, primary_key=True)
    standard = Column(String(20), nullable=False, default="AR5")
    gas_name = Column(String(40), nullable=False)     # 甲烷／石化甲烷／氧化亞氮
    formula = Column(String(20))                      # CH4
    material_code = Column(String(12))                # 180177
    gwp100 = Column(Float, nullable=False)
    note = Column(Text)

    __table_args__ = (UniqueConstraint("standard", "gas_name"),)


# --------------------------------------------------------------------------
# 表四：活動數據
# --------------------------------------------------------------------------

class ActivityRecord(Base):
    __tablename__ = "activity_record"

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("emission_source.id"), nullable=False)

    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    raw_quantity = Column(Float, nullable=False)      # 單據上的原始數字
    unit = Column(String(20), nullable=False)

    # 跨期分攤（帳單期間常跨年度）
    allocation_ratio = Column(Float, default=1.0)
    allocated_quantity = Column(Float)

    # 表四／表六 品質欄位
    data_quality = Column(Enum(DataQuality), default=DataQuality.MEASURED)
    estimation_basis = Column(Text)         # 推估時必填，由服務層強制
    data_source = Column(String(120))       # 電費單／領料單／發票
    keeping_unit = Column(String(80))       # 保存單位
    measure_frequency = Column(String(80))
    calc_method = Column(String(160))       # 排放量計算方法

    evidence_type = Column(Enum(EvidenceType), default=EvidenceType.MANUAL)
    evidence_ref = Column(String(255))
    evidence_file = Column(String(255))
    raw_text = Column(Text)                 # OCR／發票原始文字

    status = Column(Enum(RecordStatus), default=RecordStatus.DRAFT)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    source = relationship("EmissionSource", back_populates="records")
    result = relationship("EmissionResult", back_populates="record", uselist=False)


# --------------------------------------------------------------------------
# 表五：定量盤查結果（快照）
# --------------------------------------------------------------------------

class EmissionResult(Base):
    __tablename__ = "emission_result"

    id = Column(Integer, primary_key=True)
    record_id = Column(Integer, ForeignKey("activity_record.id"), unique=True)

    co2_kg = Column(Float, default=0.0)
    ch4_co2e_kg = Column(Float, default=0.0)
    n2o_co2e_kg = Column(Float, default=0.0)
    total_co2e_kg = Column(Float, default=0.0)

    # ---- 稽核快照：報告產出後即使來源改版，數字仍可重現 ----
    derived_factor = Column(Float)            # 推導出的每單位合計係數
    published_factor_id = Column(Integer, ForeignKey("published_factor.id"))
    heating_value_used = Column(Float)
    heating_value_source = Column(String(200))
    electricity_factor_used = Column(Float)
    factor_set_version = Column(String(60))
    gwp_standard = Column(String(20))
    ch4_gwp_used = Column(Float)
    n2o_gwp_used = Column(Float)

    calc_trace = Column(Text)
    calculated_at = Column(DateTime, default=dt.datetime.utcnow)

    record = relationship("ActivityRecord", back_populates="result")
