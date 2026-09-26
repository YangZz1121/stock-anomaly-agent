"""运行配置。

所有 Provider 都支持 ``auto``：有密钥就用真实数据源，没有就自动降级到
构造 fixture，并在 Brief 和 README 里显式标注当前运行在降级模式。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderChoice = Literal["auto", "fuyao", "ifind", "openai", "mock"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- 扶摇金融数据 ---
    fuyao_api_key: Optional[str] = None
    fuyao_base_url: str = "https://fuyao.aicubes.cn"

    # --- iFinD MCP（证据层，预留）---
    ifind_mcp_url: Optional[str] = None
    ifind_api_key: Optional[str] = None

    # --- LLM（OpenAI 兼容协议）---
    llm_api_key: Optional[str] = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_s: float = 90.0
    llm_max_retries: int = 1

    # --- Provider 选择 ---
    market_provider: ProviderChoice = "auto"
    evidence_provider: ProviderChoice = "auto"
    llm_provider: ProviderChoice = "auto"

    # --- 规则层可调参数（显式命名，避免代码里出现魔数）---
    # 单日集中度阈值：MVP 启发式规则，非行业标准
    single_day_concentration_threshold: float = 0.60
    # 方向一致性阈值：达到该比例视为方向一致
    direction_consistency_threshold: float = 0.70
    # 路径效率阈值：高于此值视为路径单一
    path_efficiency_threshold: float = 0.70
    # 反转识别：切分前后两段累计收益的最小幅度，低于此视为噪声
    reversal_min_segment_pct: float = 0.02
    # 三层对比：个股相对行业偏离超过该值视为"明显背离"
    divergence_threshold_pct: float = 0.02
    # 行业相对市场偏离超过该值视为"行业明显变化"
    industry_move_threshold_pct: float = 0.02
    # 市场自身变化超过该值视为"大盘不稳定"
    market_move_threshold_pct: float = 0.01
    # 成交活跃度参考窗口
    turnover_baseline_days: int = 20
    # 证据扩展窗口（自然日）
    evidence_extended_window_days: int = 7

    # --- 行业与市场基准 ---
    market_index_code: str = "000001.SH"
    market_index_name: str = "上证指数"
    # 行业识别改为预置清单 + 定向核验，不再全市场扫描。
    # 这两个字段只为兼容旧环境变量，运行时不再使用。
    industry_map_max_calls: int = 1
    industry_map_ttl_hours: int = 72

    http_timeout_s: float = 20.0
    cache_dir: str = ".cache"
    fixtures_dir: str = "fixtures"
    # 慢变列表（交易日历、标的检索、行业指数、成分股）的进程/落盘缓存
    static_cache_ttl_hours: int = 12
    # 日 K 进程缓存。历史区间几乎不变；含当日的请求用较短 TTL。
    bars_cache_ttl_seconds: int = 180
    # Agent 环：规划 → 调用工具 → 观察，最多走多少步。启发式主路径约 8 步。
    agent_max_steps: int = 12
    # 首次检索之后，规划器最多还能追加几次检索（失败重试 + 反向证据）。
    agent_extra_search_limit: int = 2

    def resolved_market_provider(self) -> str:
        if self.market_provider != "auto":
            return self.market_provider
        return "fuyao" if self.fuyao_api_key else "mock"

    def resolved_evidence_provider(self) -> str:
        if self.evidence_provider != "auto":
            return self.evidence_provider
        return "ifind" if self.ifind_mcp_url else "mock"

    def resolved_llm_provider(self) -> str:
        if self.llm_provider != "auto":
            return self.llm_provider
        return "openai" if self.llm_api_key else "mock"

    def is_degraded(self) -> bool:
        return "mock" in {
            self.resolved_market_provider(),
            self.resolved_evidence_provider(),
            self.resolved_llm_provider(),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
