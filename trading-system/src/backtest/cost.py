"""
비용 모델 (수수료, 슬리피지)
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TradeCost:
    """거래 비용"""
    commission: float    # 수수료
    slippage: float      # 슬리피지 (포인트)
    total: float         # 총 비용


class CostModel(ABC):
    """비용 모델 베이스 클래스"""

    @abstractmethod
    def calculate(self, price: float, quantity: int, is_entry: bool) -> TradeCost:
        """
        거래 비용 계산

        Args:
            price: 주문 가격
            quantity: 수량
            is_entry: 진입 여부 (True: 진입, False: 청산)

        Returns:
            TradeCost: 수수료, 슬리피지, 총 비용
        """
        pass


class KOSPIMiniCostModel(CostModel):
    """
    KOSPI Mini 선물 비용 모델

    수수료:
    - 선물 수수료: 약 0.0015% (증권사마다 다름)
    - 1계약 기준: 약 500~1000원

    슬리피지:
    - KOSPI Mini 1틱 = 0.02포인트
    - 보수적으로 1~2틱 가정
    - 1포인트 = 50,000원 (KOSPI Mini 승수)

    거래세: 없음 (선물)
    """

    def __init__(
        self,
        commission_per_contract: float = 800,  # 계약당 수수료 (원)
        slippage_ticks: float = 1.0,           # 슬리피지 (틱)
        tick_size: float = 0.02,               # 1틱 크기 (포인트)
        point_value: float = 50_000            # 1포인트 가치 (원)
    ):
        self.commission_per_contract = commission_per_contract
        self.slippage_ticks = slippage_ticks
        self.tick_size = tick_size
        self.point_value = point_value

    def calculate(self, price: float, quantity: int, is_entry: bool) -> TradeCost:
        """KOSPI Mini 거래 비용 계산"""
        # 수수료 (계약당)
        commission = self.commission_per_contract * quantity

        # 슬리피지 (포인트)
        slippage_points = self.slippage_ticks * self.tick_size

        # 슬리피지 금액 (원)
        slippage_cost = slippage_points * self.point_value * quantity

        # 총 비용
        total = commission + slippage_cost

        return TradeCost(
            commission=commission,
            slippage=slippage_points,
            total=total
        )


class ZeroCostModel(CostModel):
    """비용 없는 모델 (테스트용)"""

    def calculate(self, price: float, quantity: int, is_entry: bool) -> TradeCost:
        return TradeCost(commission=0, slippage=0, total=0)


class PercentageCostModel(CostModel):
    """비율 기반 비용 모델"""

    def __init__(
        self,
        commission_rate: float = 0.0015,  # 0.15%
        slippage_rate: float = 0.0005,    # 0.05%
        point_value: float = 50_000
    ):
        self.commission_rate = commission_rate
        self.slippage_rate = slippage_rate
        self.point_value = point_value

    def calculate(self, price: float, quantity: int, is_entry: bool) -> TradeCost:
        # 계약 가치
        contract_value = price * self.point_value * quantity

        # 수수료
        commission = contract_value * self.commission_rate

        # 슬리피지 (포인트로 환산)
        slippage = price * self.slippage_rate

        # 슬리피지 금액
        slippage_cost = slippage * self.point_value * quantity

        return TradeCost(
            commission=commission,
            slippage=slippage,
            total=commission + slippage_cost
        )
