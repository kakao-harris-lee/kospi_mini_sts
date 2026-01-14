"""
================================================================================
🌙 KOSPI200 선물 야간 브리핑 시스템
================================================================================

매일 밤 글로벌 시장, 거시경제 이벤트, 수급 데이터를 종합 분석하여
다음 날 KOSPI200 선물 거래 전략을 제시합니다.

실행 시간: 매일 밤 9시~10시 (미국장 개장 후)
분석 항목:
    1. 글로벌 시장 동향 (미국 선물, 중국, 유럽, VIX)
    2. 거시경제 이벤트 캘린더
    3. 외국인/기관 수급 분석
    4. 기술적 분석 (KOSPI200 지수)
    5. 종합 판단 및 전략 제시

사용법:
    python futures_briefing_system.py

================================================================================
"""

import os
import json
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional, Tuple
from enum import Enum
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup
import warnings

warnings.filterwarnings('ignore')


# ============================================================
# 설정
# ============================================================

class FuturesConfig:
    """선물 분석 설정"""
    
    # API 키 (실제 사용 시 설정)
    FRED_API_KEY = ""           # 미국 경제지표 (https://fred.stlouisfed.org)
    ALPHA_VANTAGE_KEY = ""      # 글로벌 시장 데이터
    
    # 분석 가중치
    WEIGHT_GLOBAL = 0.35        # 글로벌 시장
    WEIGHT_FLOW = 0.30          # 수급
    WEIGHT_TECHNICAL = 0.20     # 기술적 분석
    WEIGHT_EVENT = 0.15         # 이벤트
    
    # 거래 설정
    KOSPI200_MULTIPLIER = 250000    # KOSPI200 선물 승수
    MINI_MULTIPLIER = 50000         # 미니선물 승수
    DEFAULT_STOP_LOSS_PT = 3.0      # 기본 손절 (pt)
    DEFAULT_TAKE_PROFIT_PT = 6.0    # 기본 익절 (pt)
    
    # 출력
    OUTPUT_DIR = "./futures_reports"


class MarketBias(Enum):
    """시장 방향성"""
    STRONG_BULLISH = "강세"
    BULLISH = "약간 강세"
    NEUTRAL = "중립"
    BEARISH = "약간 약세"
    STRONG_BEARISH = "약세"


class SignalStrength(Enum):
    """신호 강도"""
    STRONG = "강함"
    MODERATE = "보통"
    WEAK = "약함"


@dataclass
class GlobalMarketData:
    """글로벌 시장 데이터"""
    # 미국
    sp500_futures: float = 0.0
    nasdaq_futures: float = 0.0
    dow_futures: float = 0.0
    sp500_change_pct: float = 0.0
    nasdaq_change_pct: float = 0.0
    
    # 변동성/리스크
    vix: float = 0.0
    vix_change: float = 0.0
    
    # 환율
    usd_krw: float = 0.0
    usd_krw_change: float = 0.0
    dollar_index: float = 0.0
    
    # 아시아
    china_csi300: float = 0.0
    china_change_pct: float = 0.0
    japan_nikkei: float = 0.0
    japan_change_pct: float = 0.0
    
    # 유럽
    euro_stoxx: float = 0.0
    euro_change_pct: float = 0.0
    
    # 원자재
    wti_oil: float = 0.0
    gold: float = 0.0
    
    # 종합 점수
    global_score: float = 0.0
    global_bias: MarketBias = MarketBias.NEUTRAL


@dataclass
class EconomicEvent:
    """경제 이벤트"""
    date: str
    time: str
    country: str
    event: str
    importance: str  # "높음", "중간", "낮음"
    previous: str = ""
    forecast: str = ""
    actual: str = ""
    impact_analysis: str = ""


@dataclass
class FlowData:
    """수급 데이터"""
    # 외국인
    foreign_futures_net: float = 0.0      # 선물 순매수 (계약)
    foreign_futures_5d: float = 0.0       # 5일 누적
    foreign_futures_20d: float = 0.0      # 20일 누적
    foreign_call_net: float = 0.0         # 콜옵션 순매수
    foreign_put_net: float = 0.0          # 풋옵션 순매수
    
    # 기관
    institution_futures_net: float = 0.0
    institution_5d: float = 0.0
    
    # 개인
    retail_futures_net: float = 0.0
    
    # 프로그램
    program_buy: float = 0.0
    program_sell: float = 0.0
    program_net: float = 0.0
    
    # 베이시스
    basis: float = 0.0                    # 현물-선물 괴리
    basis_rate: float = 0.0               # 괴리율 (%)
    theoretical_price: float = 0.0
    
    # 종합 점수
    flow_score: float = 0.0
    flow_bias: MarketBias = MarketBias.NEUTRAL


@dataclass
class TechnicalData:
    """기술적 분석 데이터"""
    # KOSPI200 지수
    index_price: float = 0.0
    index_change_pct: float = 0.0
    
    # 이동평균
    ma5: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    ma120: float = 0.0
    
    # 추세
    trend_short: str = ""     # 단기 (5일)
    trend_mid: str = ""       # 중기 (20일)
    trend_long: str = ""      # 장기 (60일)
    
    # 모멘텀
    rsi: float = 0.0
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_hist: float = 0.0
    
    # 변동성
    atr: float = 0.0          # Average True Range
    bb_upper: float = 0.0     # 볼린저밴드 상단
    bb_lower: float = 0.0     # 볼린저밴드 하단
    bb_position: float = 0.0  # 밴드 내 위치 (0~1)
    
    # 지지/저항
    support_1: float = 0.0
    support_2: float = 0.0
    resistance_1: float = 0.0
    resistance_2: float = 0.0
    
    # 피봇
    pivot: float = 0.0
    
    # 종합 점수
    technical_score: float = 0.0
    technical_bias: MarketBias = MarketBias.NEUTRAL


@dataclass
class TradingStrategy:
    """거래 전략"""
    direction: str              # "롱", "숏", "관망"
    confidence: str             # "높음", "중간", "낮음"
    entry_condition: str        # 진입 조건
    entry_price: float          # 예상 진입가
    stop_loss: float            # 손절가
    take_profit: float          # 익절가
    position_size: str          # "풀", "하프", "쿼터"
    time_horizon: str           # "장중", "오버나이트", "스윙"
    key_levels: List[float] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)
    catalysts: List[str] = field(default_factory=list)


@dataclass
class FuturesBriefing:
    """선물 브리핑 종합"""
    date: str
    generation_time: str
    
    # 각 분석 데이터
    global_market: GlobalMarketData
    economic_events: List[EconomicEvent]
    flow_data: FlowData
    technical: TechnicalData
    
    # 종합 판단
    overall_score: float            # -100 ~ +100
    overall_bias: MarketBias
    signal_strength: SignalStrength
    
    # 전략
    primary_strategy: TradingStrategy
    alternative_strategy: Optional[TradingStrategy]
    
    # 요약
    summary: str
    key_points: List[str]
    risk_warning: str


# ============================================================
# 데이터 수집 모듈
# ============================================================

class GlobalMarketCollector:
    """글로벌 시장 데이터 수집"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def collect(self) -> GlobalMarketData:
        """글로벌 시장 데이터 수집"""
        data = GlobalMarketData()
        
        # 실제 API 연동 (예시)
        # data = self._fetch_from_api()
        
        # 샘플 데이터 (API 없을 때)
        data = self._get_sample_data()
        
        # 점수 계산
        data.global_score, data.global_bias = self._calculate_score(data)
        
        return data
    
    def _fetch_from_api(self) -> GlobalMarketData:
        """실제 API에서 데이터 수집"""
        data = GlobalMarketData()
        
        try:
            # Yahoo Finance 또는 다른 소스에서 데이터 수집
            # 실제 구현 시 여기에 API 호출 코드 추가
            pass
        except Exception as e:
            print(f"⚠️ API 데이터 수집 실패: {e}")
        
        return data
    
    def _get_sample_data(self) -> GlobalMarketData:
        """샘플 데이터 생성"""
        np.random.seed(int(datetime.now().timestamp()) % 1000)
        
        # 현실적인 범위의 랜덤 데이터
        sp500_base = 5800 + np.random.uniform(-100, 100)
        nasdaq_base = 20500 + np.random.uniform(-300, 300)
        
        return GlobalMarketData(
            # 미국 선물
            sp500_futures=sp500_base,
            nasdaq_futures=nasdaq_base,
            dow_futures=42000 + np.random.uniform(-200, 200),
            sp500_change_pct=np.random.uniform(-1.5, 1.5),
            nasdaq_change_pct=np.random.uniform(-2.0, 2.0),
            
            # VIX
            vix=15 + np.random.uniform(-5, 10),
            vix_change=np.random.uniform(-2, 2),
            
            # 환율
            usd_krw=1380 + np.random.uniform(-20, 20),
            usd_krw_change=np.random.uniform(-1, 1),
            dollar_index=104 + np.random.uniform(-2, 2),
            
            # 아시아
            china_csi300=3800 + np.random.uniform(-50, 50),
            china_change_pct=np.random.uniform(-2, 2),
            japan_nikkei=38000 + np.random.uniform(-500, 500),
            japan_change_pct=np.random.uniform(-1.5, 1.5),
            
            # 유럽
            euro_stoxx=4800 + np.random.uniform(-50, 50),
            euro_change_pct=np.random.uniform(-1, 1),
            
            # 원자재
            wti_oil=75 + np.random.uniform(-5, 5),
            gold=2650 + np.random.uniform(-30, 30)
        )
    
    def _calculate_score(self, data: GlobalMarketData) -> Tuple[float, MarketBias]:
        """글로벌 시장 점수 계산"""
        score = 0
        
        # 미국 선물 (가중치 40%)
        if data.sp500_change_pct > 0.5:
            score += 20
        elif data.sp500_change_pct > 0:
            score += 10
        elif data.sp500_change_pct < -0.5:
            score -= 20
        elif data.sp500_change_pct < 0:
            score -= 10
        
        if data.nasdaq_change_pct > 0.5:
            score += 15
        elif data.nasdaq_change_pct < -0.5:
            score -= 15
        
        # VIX (가중치 20%)
        if data.vix < 15:
            score += 10
        elif data.vix > 25:
            score -= 15
        elif data.vix > 20:
            score -= 10
        
        if data.vix_change > 2:
            score -= 10
        elif data.vix_change < -2:
            score += 5
        
        # 환율 (가중치 15%)
        if data.usd_krw_change > 0.5:  # 원화 약세 = 외국인 이탈 우려
            score -= 10
        elif data.usd_krw_change < -0.5:  # 원화 강세 = 외국인 유입 기대
            score += 10
        
        # 아시아 (가중치 15%)
        if data.china_change_pct > 1:
            score += 10
        elif data.china_change_pct < -1:
            score -= 10
        
        if data.japan_change_pct > 0.5:
            score += 5
        elif data.japan_change_pct < -0.5:
            score -= 5
        
        # 유럽 (가중치 10%)
        if data.euro_change_pct > 0.5:
            score += 5
        elif data.euro_change_pct < -0.5:
            score -= 5
        
        # 점수를 -100 ~ +100으로 정규화
        score = max(-100, min(100, score * 1.5))
        
        # 방향성 결정
        if score >= 30:
            bias = MarketBias.STRONG_BULLISH
        elif score >= 10:
            bias = MarketBias.BULLISH
        elif score <= -30:
            bias = MarketBias.STRONG_BEARISH
        elif score <= -10:
            bias = MarketBias.BEARISH
        else:
            bias = MarketBias.NEUTRAL
        
        return score, bias


class EconomicEventCollector:
    """경제 이벤트 수집"""
    
    def __init__(self):
        self.session = requests.Session()
    
    def collect(self, days_ahead: int = 3) -> List[EconomicEvent]:
        """향후 N일간 경제 이벤트 수집"""
        events = []
        
        # 실제로는 Investing.com, ForexFactory 등에서 크롤링
        # 여기서는 주요 이벤트 샘플 제공
        events = self._get_sample_events(days_ahead)
        
        return events
    
    def _get_sample_events(self, days_ahead: int) -> List[EconomicEvent]:
        """샘플 이벤트 데이터"""
        today = datetime.now()
        
        # 실제 주요 이벤트 유형들
        major_events = [
            # FOMC 관련
            {
                "country": "미국",
                "event": "FOMC 금리결정",
                "importance": "높음",
                "impact": "금리 동결 시 중립, 인하 시 강세, 인상 시 약세"
            },
            {
                "country": "미국",
                "event": "FOMC 의사록 공개",
                "importance": "높음",
                "impact": "매파적 발언 시 약세, 비둘기파적 발언 시 강세"
            },
            # 고용
            {
                "country": "미국",
                "event": "비농업 고용지표",
                "importance": "높음",
                "impact": "예상 상회 시 금리 인상 우려로 단기 약세"
            },
            {
                "country": "미국",
                "event": "실업률",
                "importance": "높음",
                "impact": "예상 하회 시 경기 호조, 상회 시 침체 우려"
            },
            # 물가
            {
                "country": "미국",
                "event": "소비자물가지수(CPI)",
                "importance": "높음",
                "impact": "예상 상회 시 금리 인상 우려, 하회 시 강세"
            },
            {
                "country": "미국",
                "event": "생산자물가지수(PPI)",
                "importance": "중간",
                "impact": "CPI 선행지표로 방향성 참고"
            },
            # 경기
            {
                "country": "미국",
                "event": "GDP 성장률",
                "importance": "높음",
                "impact": "예상 상회 시 강세, 마이너스 시 침체 우려"
            },
            {
                "country": "미국",
                "event": "ISM 제조업지수",
                "importance": "중간",
                "impact": "50 상회 시 확장, 하회 시 위축"
            },
            # 한국
            {
                "country": "한국",
                "event": "한국은행 기준금리 결정",
                "importance": "높음",
                "impact": "인하 시 원화 약세 + 유동성 강세"
            },
            {
                "country": "한국",
                "event": "수출입 동향",
                "importance": "중간",
                "impact": "수출 증가 시 경기 기대감"
            },
            # 중국
            {
                "country": "중국",
                "event": "제조업 PMI",
                "importance": "중간",
                "impact": "50 상회 시 한국 수출 기대감"
            },
        ]
        
        events = []
        np.random.seed(int(datetime.now().timestamp()) % 1000)
        
        for i in range(days_ahead):
            date = (today + timedelta(days=i)).strftime("%Y-%m-%d")
            
            # 하루에 1~3개 이벤트 배치
            day_events = np.random.choice(len(major_events), 
                                          size=min(3, np.random.randint(1, 4)), 
                                          replace=False)
            
            for idx in day_events:
                evt = major_events[idx]
                events.append(EconomicEvent(
                    date=date,
                    time=f"{np.random.randint(8, 23):02d}:{np.random.choice(['00', '30'])}",
                    country=evt["country"],
                    event=evt["event"],
                    importance=evt["importance"],
                    previous=f"{np.random.uniform(0.5, 5):.1f}%",
                    forecast=f"{np.random.uniform(0.5, 5):.1f}%",
                    impact_analysis=evt["impact"]
                ))
        
        # 날짜순 정렬
        events.sort(key=lambda x: (x.date, x.time))
        
        return events


class FlowDataCollector:
    """수급 데이터 수집"""
    
    def __init__(self):
        self.session = requests.Session()
    
    def collect(self) -> FlowData:
        """수급 데이터 수집"""
        data = FlowData()
        
        # 실제로는 KRX, 증권사 API 등에서 수집
        # 여기서는 샘플 데이터
        data = self._get_sample_data()
        
        # 점수 계산
        data.flow_score, data.flow_bias = self._calculate_score(data)
        
        return data
    
    def _get_sample_data(self) -> FlowData:
        """샘플 수급 데이터"""
        np.random.seed(int(datetime.now().timestamp()) % 1000 + 1)
        
        foreign_net = np.random.uniform(-15000, 15000)
        
        return FlowData(
            # 외국인
            foreign_futures_net=foreign_net,
            foreign_futures_5d=foreign_net * 5 * np.random.uniform(0.5, 1.5),
            foreign_futures_20d=foreign_net * 20 * np.random.uniform(0.3, 1.2),
            foreign_call_net=np.random.uniform(-5000, 5000),
            foreign_put_net=np.random.uniform(-5000, 5000),
            
            # 기관
            institution_futures_net=np.random.uniform(-10000, 10000),
            institution_5d=np.random.uniform(-30000, 30000),
            
            # 개인
            retail_futures_net=-foreign_net * np.random.uniform(0.3, 0.7),
            
            # 프로그램
            program_buy=np.random.uniform(3000, 8000),
            program_sell=np.random.uniform(3000, 8000),
            program_net=np.random.uniform(-3000, 3000),
            
            # 베이시스
            basis=np.random.uniform(-2, 2),
            basis_rate=np.random.uniform(-0.5, 0.5),
            theoretical_price=350 + np.random.uniform(-5, 5)
        )
    
    def _calculate_score(self, data: FlowData) -> Tuple[float, MarketBias]:
        """수급 점수 계산"""
        score = 0
        
        # 외국인 선물 (가중치 50%)
        if data.foreign_futures_net > 5000:
            score += 25
        elif data.foreign_futures_net > 2000:
            score += 15
        elif data.foreign_futures_net < -5000:
            score -= 25
        elif data.foreign_futures_net < -2000:
            score -= 15
        
        # 외국인 5일 누적 (추세 확인)
        if data.foreign_futures_5d > 20000:
            score += 15
        elif data.foreign_futures_5d < -20000:
            score -= 15
        
        # 옵션 포지션 (합성 포지션 분석)
        # 콜 매수 > 풋 매수 = 강세 뷰
        option_diff = data.foreign_call_net - data.foreign_put_net
        if option_diff > 3000:
            score += 10
        elif option_diff < -3000:
            score -= 10
        
        # 기관 (가중치 25%)
        if data.institution_futures_net > 3000:
            score += 10
        elif data.institution_futures_net < -3000:
            score -= 10
        
        # 베이시스 (가중치 15%)
        # 고평가(양의 베이시스) = 선물 매도 압력
        # 저평가(음의 베이시스) = 선물 매수 기회
        if data.basis < -1:
            score += 10  # 저평가, 매수 기회
        elif data.basis > 1:
            score -= 10  # 고평가, 매도 압력
        
        # 프로그램 (가중치 10%)
        if data.program_net > 2000:
            score += 5
        elif data.program_net < -2000:
            score -= 5
        
        # 점수 정규화
        score = max(-100, min(100, score * 1.3))
        
        # 방향성 결정
        if score >= 30:
            bias = MarketBias.STRONG_BULLISH
        elif score >= 10:
            bias = MarketBias.BULLISH
        elif score <= -30:
            bias = MarketBias.STRONG_BEARISH
        elif score <= -10:
            bias = MarketBias.BEARISH
        else:
            bias = MarketBias.NEUTRAL
        
        return score, bias


class TechnicalAnalyzer:
    """기술적 분석"""
    
    def __init__(self):
        pass
    
    def analyze(self, df: pd.DataFrame = None) -> TechnicalData:
        """기술적 분석 수행"""
        if df is None:
            df = self._get_sample_data()
        
        data = TechnicalData()
        
        # 현재가
        data.index_price = df['close'].iloc[-1]
        data.index_change_pct = (df['close'].iloc[-1] / df['close'].iloc[-2] - 1) * 100
        
        # 이동평균
        data.ma5 = df['close'].rolling(5).mean().iloc[-1]
        data.ma20 = df['close'].rolling(20).mean().iloc[-1]
        data.ma60 = df['close'].rolling(60).mean().iloc[-1] if len(df) >= 60 else data.ma20
        data.ma120 = df['close'].rolling(120).mean().iloc[-1] if len(df) >= 120 else data.ma60
        
        # 추세 판단
        data.trend_short = "상승" if data.index_price > data.ma5 else "하락"
        data.trend_mid = "상승" if data.ma5 > data.ma20 else "하락"
        data.trend_long = "상승" if data.ma20 > data.ma60 else "하락"
        
        # RSI
        data.rsi = self._calculate_rsi(df['close'])
        
        # MACD
        data.macd, data.macd_signal, data.macd_hist = self._calculate_macd(df['close'])
        
        # 볼린저 밴드
        data.bb_upper, data.bb_lower, data.bb_position = self._calculate_bollinger(df['close'])
        
        # ATR
        data.atr = self._calculate_atr(df)
        
        # 피봇 & 지지/저항
        data.pivot = (df['high'].iloc[-1] + df['low'].iloc[-1] + df['close'].iloc[-1]) / 3
        data.resistance_1 = 2 * data.pivot - df['low'].iloc[-1]
        data.resistance_2 = data.pivot + (df['high'].iloc[-1] - df['low'].iloc[-1])
        data.support_1 = 2 * data.pivot - df['high'].iloc[-1]
        data.support_2 = data.pivot - (df['high'].iloc[-1] - df['low'].iloc[-1])
        
        # 점수 계산
        data.technical_score, data.technical_bias = self._calculate_score(data)
        
        return data
    
    def _get_sample_data(self) -> pd.DataFrame:
        """샘플 KOSPI200 데이터"""
        np.random.seed(int(datetime.now().timestamp()) % 1000 + 2)
        
        days = 120
        dates = pd.date_range(end=datetime.now(), periods=days, freq='B')
        
        # 랜덤 워크 + 추세
        base_price = 350
        returns = np.random.normal(0.0005, 0.012, days)
        prices = base_price * np.exp(np.cumsum(returns))
        
        df = pd.DataFrame({
            'date': dates,
            'open': prices * (1 + np.random.uniform(-0.005, 0.005, days)),
            'high': prices * (1 + np.random.uniform(0, 0.015, days)),
            'low': prices * (1 - np.random.uniform(0, 0.015, days)),
            'close': prices,
            'volume': np.random.randint(50000, 200000, days)
        })
        
        return df
    
    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> float:
        """RSI 계산"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi.iloc[-1], 2)
    
    def _calculate_macd(self, prices: pd.Series) -> Tuple[float, float, float]:
        """MACD 계산"""
        ema12 = prices.ewm(span=12, adjust=False).mean()
        ema26 = prices.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        hist = macd - signal
        return round(macd.iloc[-1], 4), round(signal.iloc[-1], 4), round(hist.iloc[-1], 4)
    
    def _calculate_bollinger(self, prices: pd.Series, period: int = 20) -> Tuple[float, float, float]:
        """볼린저 밴드 계산"""
        ma = prices.rolling(window=period).mean()
        std = prices.rolling(window=period).std()
        upper = ma + 2 * std
        lower = ma - 2 * std
        
        current = prices.iloc[-1]
        position = (current - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1])
        
        return round(upper.iloc[-1], 2), round(lower.iloc[-1], 2), round(position, 2)
    
    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """ATR 계산"""
        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift())
        low_close = abs(df['low'] - df['close'].shift())
        
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()
        
        return round(atr.iloc[-1], 2)
    
    def _calculate_score(self, data: TechnicalData) -> Tuple[float, MarketBias]:
        """기술적 점수 계산"""
        score = 0
        
        # 추세 (가중치 40%)
        if data.trend_short == "상승":
            score += 10
        else:
            score -= 10
            
        if data.trend_mid == "상승":
            score += 15
        else:
            score -= 15
            
        if data.trend_long == "상승":
            score += 15
        else:
            score -= 15
        
        # RSI (가중치 25%)
        if data.rsi < 30:
            score += 15  # 과매도, 반등 기대
        elif data.rsi < 40:
            score += 5
        elif data.rsi > 70:
            score -= 15  # 과매수, 조정 우려
        elif data.rsi > 60:
            score -= 5
        
        # MACD (가중치 20%)
        if data.macd_hist > 0:
            score += 10
            if data.macd_hist > data.macd * 0.1:  # 히스토그램 확대
                score += 5
        else:
            score -= 10
            if data.macd_hist < data.macd * 0.1:
                score -= 5
        
        # 볼린저 위치 (가중치 15%)
        if data.bb_position < 0.2:
            score += 10  # 하단 근접, 반등 기대
        elif data.bb_position > 0.8:
            score -= 10  # 상단 근접, 조정 우려
        
        # 점수 정규화
        score = max(-100, min(100, score * 1.2))
        
        # 방향성 결정
        if score >= 30:
            bias = MarketBias.STRONG_BULLISH
        elif score >= 10:
            bias = MarketBias.BULLISH
        elif score <= -30:
            bias = MarketBias.STRONG_BEARISH
        elif score <= -10:
            bias = MarketBias.BEARISH
        else:
            bias = MarketBias.NEUTRAL
        
        return score, bias


# ============================================================
# 전략 생성기
# ============================================================

class StrategyGenerator:
    """거래 전략 생성"""
    
    def __init__(self):
        pass
    
    def generate(
        self,
        global_data: GlobalMarketData,
        events: List[EconomicEvent],
        flow_data: FlowData,
        technical: TechnicalData,
        overall_score: float
    ) -> Tuple[TradingStrategy, Optional[TradingStrategy]]:
        """종합 전략 생성"""
        
        # 방향 결정
        if overall_score >= 25:
            direction = "롱"
            confidence = "높음" if overall_score >= 40 else "중간"
        elif overall_score <= -25:
            direction = "숏"
            confidence = "높음" if overall_score <= -40 else "중간"
        else:
            direction = "관망"
            confidence = "낮음"
        
        # 진입 조건
        if direction == "롱":
            entry_condition = self._generate_long_entry(technical)
            entry_price = technical.index_price
            stop_loss = entry_price - FuturesConfig.DEFAULT_STOP_LOSS_PT
            take_profit = entry_price + FuturesConfig.DEFAULT_TAKE_PROFIT_PT
        elif direction == "숏":
            entry_condition = self._generate_short_entry(technical)
            entry_price = technical.index_price
            stop_loss = entry_price + FuturesConfig.DEFAULT_STOP_LOSS_PT
            take_profit = entry_price - FuturesConfig.DEFAULT_TAKE_PROFIT_PT
        else:
            entry_condition = "조건 충족 시까지 대기"
            entry_price = technical.index_price
            stop_loss = 0
            take_profit = 0
        
        # 포지션 사이징
        if confidence == "높음":
            position_size = "풀"
        elif confidence == "중간":
            position_size = "하프"
        else:
            position_size = "쿼터"
        
        # 시간 범위
        high_impact_events = [e for e in events if e.importance == "높음" and e.date == datetime.now().strftime("%Y-%m-%d")]
        if high_impact_events:
            time_horizon = "장중"  # 이벤트 전 청산
        else:
            time_horizon = "오버나이트"
        
        # 핵심 레벨
        key_levels = [
            technical.pivot,
            technical.support_1,
            technical.resistance_1
        ]
        
        # 리스크 요인
        risk_factors = self._identify_risks(global_data, events, flow_data, technical)
        
        # 촉매
        catalysts = self._identify_catalysts(global_data, events, flow_data)
        
        primary = TradingStrategy(
            direction=direction,
            confidence=confidence,
            entry_condition=entry_condition,
            entry_price=round(entry_price, 2),
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            position_size=position_size,
            time_horizon=time_horizon,
            key_levels=[round(l, 2) for l in key_levels],
            risk_factors=risk_factors,
            catalysts=catalysts
        )
        
        # 대안 전략 (반대 시나리오)
        alternative = None
        if direction != "관망":
            alt_direction = "숏" if direction == "롱" else "롱"
            alternative = TradingStrategy(
                direction=alt_direction,
                confidence="낮음",
                entry_condition=f"주요 지지/저항 이탈 시 전환",
                entry_price=technical.support_1 if direction == "롱" else technical.resistance_1,
                stop_loss=technical.support_2 if direction == "롱" else technical.resistance_2,
                take_profit=technical.pivot,
                position_size="쿼터",
                time_horizon="장중",
                key_levels=key_levels,
                risk_factors=["메인 시나리오 실패 시 대응"],
                catalysts=[]
            )
        
        return primary, alternative
    
    def _generate_long_entry(self, technical: TechnicalData) -> str:
        """롱 진입 조건 생성"""
        conditions = []
        
        if technical.index_price < technical.ma5:
            conditions.append(f"5일선({technical.ma5:.2f}) 돌파 시")
        else:
            conditions.append(f"현재가({technical.index_price:.2f}) 유지 시 시가 진입")
        
        if technical.rsi < 50:
            conditions.append(f"RSI {technical.rsi:.0f} → 50 상향 돌파 확인")
        
        return " / ".join(conditions)
    
    def _generate_short_entry(self, technical: TechnicalData) -> str:
        """숏 진입 조건 생성"""
        conditions = []
        
        if technical.index_price > technical.ma5:
            conditions.append(f"5일선({technical.ma5:.2f}) 이탈 시")
        else:
            conditions.append(f"현재가({technical.index_price:.2f}) 유지 시 시가 진입")
        
        if technical.rsi > 50:
            conditions.append(f"RSI {technical.rsi:.0f} → 50 하향 돌파 확인")
        
        return " / ".join(conditions)
    
    def _identify_risks(
        self,
        global_data: GlobalMarketData,
        events: List[EconomicEvent],
        flow_data: FlowData,
        technical: TechnicalData
    ) -> List[str]:
        """리스크 요인 식별"""
        risks = []
        
        # VIX 리스크
        if global_data.vix > 20:
            risks.append(f"VIX {global_data.vix:.1f} 상승 - 변동성 확대 주의")
        
        # 이벤트 리스크
        high_events = [e for e in events[:3] if e.importance == "높음"]
        if high_events:
            risks.append(f"주요 이벤트: {high_events[0].event} ({high_events[0].date})")
        
        # 수급 리스크
        if abs(flow_data.foreign_futures_net) > 10000:
            direction = "매수" if flow_data.foreign_futures_net > 0 else "매도"
            risks.append(f"외국인 대량 {direction} - 추세 강화 또는 반전 주의")
        
        # 기술적 리스크
        if technical.rsi > 70:
            risks.append(f"RSI {technical.rsi:.0f} 과매수 - 조정 가능성")
        elif technical.rsi < 30:
            risks.append(f"RSI {technical.rsi:.0f} 과매도 - 추가 하락 가능성")
        
        # 환율 리스크
        if abs(global_data.usd_krw_change) > 1:
            risks.append(f"환율 급변동 ({global_data.usd_krw_change:+.1f}%) - 외국인 수급 변화 주의")
        
        return risks[:5]  # 최대 5개
    
    def _identify_catalysts(
        self,
        global_data: GlobalMarketData,
        events: List[EconomicEvent],
        flow_data: FlowData
    ) -> List[str]:
        """상승/하락 촉매 식별"""
        catalysts = []
        
        # 미국 시장
        if global_data.sp500_change_pct > 0.5:
            catalysts.append(f"미국 증시 강세 ({global_data.sp500_change_pct:+.1f}%)")
        elif global_data.sp500_change_pct < -0.5:
            catalysts.append(f"미국 증시 약세 ({global_data.sp500_change_pct:+.1f}%)")
        
        # 외국인 수급
        if flow_data.foreign_futures_5d > 15000:
            catalysts.append(f"외국인 5일 연속 순매수 ({flow_data.foreign_futures_5d:,.0f} 계약)")
        elif flow_data.foreign_futures_5d < -15000:
            catalysts.append(f"외국인 5일 연속 순매도 ({flow_data.foreign_futures_5d:,.0f} 계약)")
        
        # 베이시스
        if flow_data.basis < -1:
            catalysts.append(f"선물 저평가 (베이시스 {flow_data.basis:.2f}pt)")
        elif flow_data.basis > 1:
            catalysts.append(f"선물 고평가 (베이시스 {flow_data.basis:.2f}pt)")
        
        return catalysts


# ============================================================
# 리포트 생성기
# ============================================================

class BriefingReportGenerator:
    """브리핑 리포트 생성"""
    
    def __init__(self):
        self.date = datetime.now().strftime("%Y%m%d")
        os.makedirs(FuturesConfig.OUTPUT_DIR, exist_ok=True)
    
    def generate(self, briefing: FuturesBriefing) -> str:
        """마크다운 리포트 생성"""
        
        # 방향성 이모지
        bias_emoji = {
            MarketBias.STRONG_BULLISH: "🟢🟢",
            MarketBias.BULLISH: "🟢",
            MarketBias.NEUTRAL: "⚪",
            MarketBias.BEARISH: "🔴",
            MarketBias.STRONG_BEARISH: "🔴🔴"
        }
        
        report = f"""# 🌙 KOSPI200 선물 야간 브리핑

**생성일시**: {briefing.generation_time}  
**분석대상**: KOSPI200 선물 (F) / 미니선물 (MF)

---

## 📊 종합 판단

| 항목 | 값 |
|------|-----|
| **종합 점수** | {self._create_score_bar(briefing.overall_score)} **{briefing.overall_score:+.0f}점** |
| **시장 방향** | {bias_emoji.get(briefing.overall_bias, '⚪')} **{briefing.overall_bias.value}** |
| **신호 강도** | {briefing.signal_strength.value} |

### 핵심 포인트
"""
        
        for point in briefing.key_points:
            report += f"- {point}\n"
        
        report += f"""
---

## 🎯 거래 전략

### 주요 전략: {briefing.primary_strategy.direction}

| 항목 | 값 |
|------|-----|
| **방향** | {'📈 롱' if briefing.primary_strategy.direction == '롱' else '📉 숏' if briefing.primary_strategy.direction == '숏' else '⏸️ 관망'} |
| **신뢰도** | {briefing.primary_strategy.confidence} |
| **진입 조건** | {briefing.primary_strategy.entry_condition} |
| **예상 진입가** | {briefing.primary_strategy.entry_price:.2f} |
| **손절가** | {briefing.primary_strategy.stop_loss:.2f} |
| **익절가** | {briefing.primary_strategy.take_profit:.2f} |
| **포지션** | {briefing.primary_strategy.position_size} |
| **보유 기간** | {briefing.primary_strategy.time_horizon} |

**핵심 레벨**: {', '.join([f'{l:.2f}' for l in briefing.primary_strategy.key_levels])}

**상승 촉매**:
"""
        
        for catalyst in briefing.primary_strategy.catalysts:
            report += f"- {catalyst}\n"
        
        report += f"""
**리스크 요인**:
"""
        
        for risk in briefing.primary_strategy.risk_factors:
            report += f"- ⚠️ {risk}\n"
        
        if briefing.alternative_strategy:
            report += f"""
### 대안 전략 (시나리오 B)

- **방향**: {briefing.alternative_strategy.direction}
- **조건**: {briefing.alternative_strategy.entry_condition}
- **진입가**: {briefing.alternative_strategy.entry_price:.2f}

---
"""
        
        report += f"""
## 🌍 글로벌 시장

### 미국 시장
| 지수 | 현재가 | 등락률 |
|------|--------|--------|
| S&P500 선물 | {briefing.global_market.sp500_futures:,.0f} | {briefing.global_market.sp500_change_pct:+.2f}% |
| 나스닥 선물 | {briefing.global_market.nasdaq_futures:,.0f} | {briefing.global_market.nasdaq_change_pct:+.2f}% |
| VIX | {briefing.global_market.vix:.1f} | {briefing.global_market.vix_change:+.1f} |

### 환율
| 항목 | 현재가 | 등락률 |
|------|--------|--------|
| USD/KRW | {briefing.global_market.usd_krw:,.1f} | {briefing.global_market.usd_krw_change:+.2f}% |
| 달러인덱스 | {briefing.global_market.dollar_index:.1f} | - |

### 아시아/유럽
| 지수 | 등락률 |
|------|--------|
| 중국 CSI300 | {briefing.global_market.china_change_pct:+.2f}% |
| 일본 니케이 | {briefing.global_market.japan_change_pct:+.2f}% |
| 유로스톡스 | {briefing.global_market.euro_change_pct:+.2f}% |

**글로벌 점수**: {briefing.global_market.global_score:+.0f}점 ({briefing.global_market.global_bias.value})

---

## 💰 수급 분석

### 외국인
| 항목 | 수량 (계약) |
|------|------------|
| 선물 순매수 (당일) | {briefing.flow_data.foreign_futures_net:+,.0f} |
| 선물 순매수 (5일) | {briefing.flow_data.foreign_futures_5d:+,.0f} |
| 콜옵션 순매수 | {briefing.flow_data.foreign_call_net:+,.0f} |
| 풋옵션 순매수 | {briefing.flow_data.foreign_put_net:+,.0f} |

### 기관/개인
| 주체 | 순매수 (계약) |
|------|-------------|
| 기관 | {briefing.flow_data.institution_futures_net:+,.0f} |
| 개인 | {briefing.flow_data.retail_futures_net:+,.0f} |

### 베이시스
| 항목 | 값 |
|------|-----|
| 베이시스 | {briefing.flow_data.basis:+.2f} pt |
| 괴리율 | {briefing.flow_data.basis_rate:+.2f}% |

**수급 점수**: {briefing.flow_data.flow_score:+.0f}점 ({briefing.flow_data.flow_bias.value})

---

## 📈 기술적 분석

### KOSPI200 지수
| 항목 | 값 |
|------|-----|
| 현재가 | {briefing.technical.index_price:.2f} |
| 등락률 | {briefing.technical.index_change_pct:+.2f}% |

### 이동평균
| MA | 가격 | 추세 |
|----|------|------|
| 5일 | {briefing.technical.ma5:.2f} | {briefing.technical.trend_short} |
| 20일 | {briefing.technical.ma20:.2f} | {briefing.technical.trend_mid} |
| 60일 | {briefing.technical.ma60:.2f} | {briefing.technical.trend_long} |

### 모멘텀 지표
| 지표 | 값 | 해석 |
|------|-----|------|
| RSI | {briefing.technical.rsi:.0f} | {'과매수' if briefing.technical.rsi > 70 else '과매도' if briefing.technical.rsi < 30 else '중립'} |
| MACD | {briefing.technical.macd:.4f} | {'강세' if briefing.technical.macd_hist > 0 else '약세'} |
| BB 위치 | {briefing.technical.bb_position:.0%} | {'상단' if briefing.technical.bb_position > 0.8 else '하단' if briefing.technical.bb_position < 0.2 else '중간'} |

### 지지/저항
| 레벨 | 가격 |
|------|------|
| 저항2 | {briefing.technical.resistance_2:.2f} |
| 저항1 | {briefing.technical.resistance_1:.2f} |
| **피봇** | **{briefing.technical.pivot:.2f}** |
| 지지1 | {briefing.technical.support_1:.2f} |
| 지지2 | {briefing.technical.support_2:.2f} |

**기술적 점수**: {briefing.technical.technical_score:+.0f}점 ({briefing.technical.technical_bias.value})

---

## 📅 주요 경제 이벤트

"""
        
        # 이벤트 테이블
        report += "| 날짜 | 시간 | 국가 | 이벤트 | 중요도 |\n"
        report += "|------|------|------|--------|--------|\n"
        
        for event in briefing.economic_events[:7]:
            importance_emoji = "🔴" if event.importance == "높음" else "🟡" if event.importance == "중간" else "⚪"
            report += f"| {event.date} | {event.time} | {event.country} | {event.event} | {importance_emoji} |\n"
        
        report += f"""
---

## ⚠️ 리스크 경고

{briefing.risk_warning}

---

## 📋 체크리스트

- [ ] 미국 장 마감 확인 (익일 06:00)
- [ ] 야간 뉴스 체크
- [ ] 환율 동향 확인
- [ ] 장 시작 전 글로벌 선물 재확인 (08:30)
- [ ] 손절/익절 주문 설정

---

*이 브리핑은 AI 분석 기반 참고자료입니다. 선물 거래는 높은 레버리지로 인해 원금 손실 위험이 있으며, 투자 판단의 최종 책임은 투자자 본인에게 있습니다.*
"""
        
        return report
    
    def _create_score_bar(self, score: float) -> str:
        """점수 바 시각화"""
        normalized = (score + 100) / 200
        filled = int(normalized * 10)
        empty = 10 - filled
        
        if score >= 20:
            bar_char = "🟢"
        elif score <= -20:
            bar_char = "🔴"
        else:
            bar_char = "🟡"
        
        return bar_char * filled + "⚪" * empty
    
    def save_report(self, report: str, briefing: FuturesBriefing):
        """리포트 저장"""
        # 마크다운 저장
        md_path = os.path.join(FuturesConfig.OUTPUT_DIR, f"futures_briefing_{self.date}.md")
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"📄 브리핑 리포트: {md_path}")
        
        # JSON 저장
        json_path = os.path.join(FuturesConfig.OUTPUT_DIR, f"futures_data_{self.date}.json")
        
        # dataclass를 dict로 변환
        data = {
            "date": briefing.date,
            "generation_time": briefing.generation_time,
            "overall_score": briefing.overall_score,
            "overall_bias": briefing.overall_bias.value,
            "signal_strength": briefing.signal_strength.value,
            "strategy": {
                "direction": briefing.primary_strategy.direction,
                "confidence": briefing.primary_strategy.confidence,
                "entry_price": briefing.primary_strategy.entry_price,
                "stop_loss": briefing.primary_strategy.stop_loss,
                "take_profit": briefing.primary_strategy.take_profit,
            },
            "global_score": briefing.global_market.global_score,
            "flow_score": briefing.flow_data.flow_score,
            "technical_score": briefing.technical.technical_score,
            "key_points": briefing.key_points
        }
        
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"📄 데이터 JSON: {json_path}")
        
        return md_path, json_path


# ============================================================
# 메인 시스템
# ============================================================

class FuturesBriefingSystem:
    """선물 브리핑 시스템 메인 클래스"""
    
    def __init__(self):
        self.global_collector = GlobalMarketCollector()
        self.event_collector = EconomicEventCollector()
        self.flow_collector = FlowDataCollector()
        self.technical_analyzer = TechnicalAnalyzer()
        self.strategy_generator = StrategyGenerator()
        self.report_generator = BriefingReportGenerator()
    
    def run(self) -> FuturesBriefing:
        """전체 분석 실행"""
        
        print("\n" + "=" * 70)
        print("🌙 KOSPI200 선물 야간 브리핑 시스템")
        print("=" * 70)
        print(f"실행 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        
        # ====================================================
        # 1. 글로벌 시장 데이터 수집
        # ====================================================
        print("━" * 70)
        print("📌 1단계: 글로벌 시장 분석")
        print("━" * 70)
        
        global_data = self.global_collector.collect()
        
        print(f"  미국: S&P500 {global_data.sp500_change_pct:+.2f}%, 나스닥 {global_data.nasdaq_change_pct:+.2f}%")
        print(f"  VIX: {global_data.vix:.1f} ({global_data.vix_change:+.1f})")
        print(f"  환율: {global_data.usd_krw:,.1f}원 ({global_data.usd_krw_change:+.2f}%)")
        print(f"  ✅ 글로벌 점수: {global_data.global_score:+.0f}점 ({global_data.global_bias.value})")
        
        # ====================================================
        # 2. 경제 이벤트 수집
        # ====================================================
        print("\n" + "━" * 70)
        print("📌 2단계: 경제 이벤트 캘린더")
        print("━" * 70)
        
        events = self.event_collector.collect(days_ahead=3)
        
        high_events = [e for e in events if e.importance == "높음"]
        print(f"  향후 3일간 이벤트: {len(events)}건 (고중요도: {len(high_events)}건)")
        
        for event in high_events[:3]:
            print(f"  🔴 [{event.date}] {event.event} ({event.country})")
        
        # ====================================================
        # 3. 수급 데이터 수집
        # ====================================================
        print("\n" + "━" * 70)
        print("📌 3단계: 수급 분석")
        print("━" * 70)
        
        flow_data = self.flow_collector.collect()
        
        print(f"  외국인 선물: {flow_data.foreign_futures_net:+,.0f} 계약 (5일: {flow_data.foreign_futures_5d:+,.0f})")
        print(f"  기관 선물: {flow_data.institution_futures_net:+,.0f} 계약")
        print(f"  베이시스: {flow_data.basis:+.2f}pt ({flow_data.basis_rate:+.2f}%)")
        print(f"  ✅ 수급 점수: {flow_data.flow_score:+.0f}점 ({flow_data.flow_bias.value})")
        
        # ====================================================
        # 4. 기술적 분석
        # ====================================================
        print("\n" + "━" * 70)
        print("📌 4단계: 기술적 분석")
        print("━" * 70)
        
        technical = self.technical_analyzer.analyze()
        
        print(f"  KOSPI200: {technical.index_price:.2f} ({technical.index_change_pct:+.2f}%)")
        print(f"  추세: 단기 {technical.trend_short} / 중기 {technical.trend_mid} / 장기 {technical.trend_long}")
        print(f"  RSI: {technical.rsi:.0f} / MACD: {technical.macd_hist:+.4f}")
        print(f"  피봇: {technical.pivot:.2f} (지지 {technical.support_1:.2f} / 저항 {technical.resistance_1:.2f})")
        print(f"  ✅ 기술적 점수: {technical.technical_score:+.0f}점 ({technical.technical_bias.value})")
        
        # ====================================================
        # 5. 종합 점수 계산
        # ====================================================
        print("\n" + "━" * 70)
        print("📌 5단계: 종합 판단")
        print("━" * 70)
        
        overall_score = (
            global_data.global_score * FuturesConfig.WEIGHT_GLOBAL +
            flow_data.flow_score * FuturesConfig.WEIGHT_FLOW +
            technical.technical_score * FuturesConfig.WEIGHT_TECHNICAL
        )
        
        # 이벤트 조정 (고중요도 이벤트 있으면 신뢰도 낮춤)
        if len(high_events) > 0:
            overall_score *= 0.8
        
        # 종합 방향성
        if overall_score >= 30:
            overall_bias = MarketBias.STRONG_BULLISH
            signal_strength = SignalStrength.STRONG
        elif overall_score >= 15:
            overall_bias = MarketBias.BULLISH
            signal_strength = SignalStrength.MODERATE
        elif overall_score <= -30:
            overall_bias = MarketBias.STRONG_BEARISH
            signal_strength = SignalStrength.STRONG
        elif overall_score <= -15:
            overall_bias = MarketBias.BEARISH
            signal_strength = SignalStrength.MODERATE
        else:
            overall_bias = MarketBias.NEUTRAL
            signal_strength = SignalStrength.WEAK
        
        print(f"\n  📊 종합 점수: {overall_score:+.0f}점")
        print(f"  📊 시장 방향: {overall_bias.value}")
        print(f"  📊 신호 강도: {signal_strength.value}")
        
        # ====================================================
        # 6. 전략 생성
        # ====================================================
        print("\n" + "━" * 70)
        print("📌 6단계: 전략 수립")
        print("━" * 70)
        
        primary, alternative = self.strategy_generator.generate(
            global_data, events, flow_data, technical, overall_score
        )
        
        print(f"\n  🎯 주요 전략: {primary.direction}")
        print(f"     신뢰도: {primary.confidence}")
        print(f"     진입: {primary.entry_condition}")
        print(f"     손절: {primary.stop_loss:.2f} / 익절: {primary.take_profit:.2f}")
        
        # ====================================================
        # 7. 브리핑 생성
        # ====================================================
        
        # 핵심 포인트 생성
        key_points = self._generate_key_points(global_data, flow_data, technical, events)
        
        # 리스크 경고
        risk_warning = self._generate_risk_warning(global_data, events, flow_data)
        
        # 요약
        summary = f"{overall_bias.value} 전망. {primary.direction} 전략 권장. {primary.confidence} 신뢰도."
        
        briefing = FuturesBriefing(
            date=datetime.now().strftime("%Y-%m-%d"),
            generation_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            global_market=global_data,
            economic_events=events,
            flow_data=flow_data,
            technical=technical,
            overall_score=round(overall_score, 1),
            overall_bias=overall_bias,
            signal_strength=signal_strength,
            primary_strategy=primary,
            alternative_strategy=alternative,
            summary=summary,
            key_points=key_points,
            risk_warning=risk_warning
        )
        
        # ====================================================
        # 8. 리포트 생성 및 저장
        # ====================================================
        print("\n" + "━" * 70)
        print("📌 7단계: 리포트 생성")
        print("━" * 70)
        
        report = self.report_generator.generate(briefing)
        md_path, json_path = self.report_generator.save_report(report, briefing)
        
        # ====================================================
        # 9. 최종 요약
        # ====================================================
        self._print_summary(briefing)
        
        return briefing
    
    def _generate_key_points(
        self,
        global_data: GlobalMarketData,
        flow_data: FlowData,
        technical: TechnicalData,
        events: List[EconomicEvent]
    ) -> List[str]:
        """핵심 포인트 생성"""
        points = []
        
        # 글로벌
        if abs(global_data.sp500_change_pct) > 1:
            direction = "강세" if global_data.sp500_change_pct > 0 else "약세"
            points.append(f"미국 증시 {direction} ({global_data.sp500_change_pct:+.1f}%) - 국내 시장 연동 예상")
        
        # 수급
        if abs(flow_data.foreign_futures_net) > 5000:
            direction = "순매수" if flow_data.foreign_futures_net > 0 else "순매도"
            points.append(f"외국인 선물 대량 {direction} ({flow_data.foreign_futures_net:+,.0f} 계약)")
        
        # 기술적
        if technical.rsi > 70:
            points.append(f"RSI {technical.rsi:.0f} 과매수 구간 - 단기 조정 가능성")
        elif technical.rsi < 30:
            points.append(f"RSI {technical.rsi:.0f} 과매도 구간 - 반등 기대")
        
        # 이벤트
        high_events = [e for e in events if e.importance == "높음"]
        if high_events:
            points.append(f"주요 이벤트 예정: {high_events[0].event} ({high_events[0].date})")
        
        # VIX
        if global_data.vix > 20:
            points.append(f"VIX {global_data.vix:.1f} 상승 - 변동성 확대 주의")
        
        return points[:5]
    
    def _generate_risk_warning(
        self,
        global_data: GlobalMarketData,
        events: List[EconomicEvent],
        flow_data: FlowData
    ) -> str:
        """리스크 경고 생성"""
        warnings = []
        
        if global_data.vix > 25:
            warnings.append("VIX 25 이상으로 고변동성 장세")
        
        high_events = [e for e in events if e.importance == "높음" and e.date == datetime.now().strftime("%Y-%m-%d")]
        if high_events:
            warnings.append(f"오늘 {high_events[0].event} 발표 예정")
        
        if abs(global_data.usd_krw_change) > 1:
            warnings.append("환율 급변동으로 외국인 수급 불안정")
        
        if not warnings:
            return "현재 특별한 리스크 요인 없음. 기본 리스크 관리 원칙 준수."
        
        return " / ".join(warnings) + " → 포지션 축소 또는 손절 타이트하게 설정 권장."
    
    def _print_summary(self, briefing: FuturesBriefing):
        """최종 요약 출력"""
        
        print("\n" + "=" * 70)
        print("📊 브리핑 완료!")
        print("=" * 70)
        
        direction_emoji = "📈" if briefing.primary_strategy.direction == "롱" else "📉" if briefing.primary_strategy.direction == "숏" else "⏸️"
        
        print(f"""
┌─────────────────────────────────────────────────────────────────────┐
│                    🎯 내일 선물 거래 전략                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  종합 점수: {briefing.overall_score:+.0f}점 ({briefing.overall_bias.value})
│  신호 강도: {briefing.signal_strength.value}
│                                                                      │
│  {direction_emoji} 전략: {briefing.primary_strategy.direction} ({briefing.primary_strategy.confidence} 신뢰도)
│                                                                      │
│  진입 조건: {briefing.primary_strategy.entry_condition[:40]}
│  예상 진입: {briefing.primary_strategy.entry_price:.2f}
│  손절: {briefing.primary_strategy.stop_loss:.2f} / 익절: {briefing.primary_strategy.take_profit:.2f}
│  포지션: {briefing.primary_strategy.position_size} / {briefing.primary_strategy.time_horizon}
│                                                                      │
│  핵심 레벨: {', '.join([f'{l:.2f}' for l in briefing.primary_strategy.key_levels[:3]])}
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘

📁 출력 파일:
   • ./futures_reports/futures_briefing_{datetime.now().strftime('%Y%m%d')}.md
   • ./futures_reports/futures_data_{datetime.now().strftime('%Y%m%d')}.json

⏰ 내일 체크리스트:
   • 06:00 - 미국 장 마감 확인
   • 08:30 - 글로벌 선물 재확인
   • 09:00 - 장 시작, 진입 조건 확인
   • 15:15 - 장 마감 전 포지션 정리 검토
""")


def main():
    """메인 함수"""
    system = FuturesBriefingSystem()
    briefing = system.run()


if __name__ == "__main__":
    main()
