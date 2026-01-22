"""
Test Order Validation (Minimum Quantity)

Tests for minimum order quantity enforcement across:
- OrderRequest (KIS API)
- OrderCommand (Strategy Manager)
- ORDER_COMMAND_CONTRACT (Redis Stream)
"""
import pytest
from src.collector.kis_order import OrderRequest, OrderSide, OrderType
from src.strategy.strategy_manager import OrderCommand, OrderSide as StrategySide, OrderType as StrategyOrderType, TradingMode


class TestOrderRequestValidation:
    """Test OrderRequest minimum quantity validation"""

    def test_valid_quantity(self):
        """Valid order with quantity >= 1 should be accepted"""
        # Minimum quantity (1)
        order = OrderRequest(
            symbol="101V3000",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=1
        )
        assert order.quantity == 1

        # Multiple contracts
        order2 = OrderRequest(
            symbol="101V3000",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=5,
            price=350.0
        )
        assert order2.quantity == 5

    def test_zero_quantity_rejected(self):
        """Order with quantity = 0 should be rejected"""
        with pytest.raises(ValueError, match="Order quantity must be at least 1 contract"):
            OrderRequest(
                symbol="101V3000",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=0
            )

    def test_negative_quantity_rejected(self):
        """Order with negative quantity should be rejected"""
        with pytest.raises(ValueError, match="Order quantity must be at least 1 contract"):
            OrderRequest(
                symbol="101V3000",
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=-1
            )


class TestOrderCommandValidation:
    """Test OrderCommand minimum size validation"""

    def test_valid_size(self):
        """Valid order with size >= 1 should be accepted"""
        # Minimum size (1)
        order = OrderCommand(
            symbol="101V3000",
            side=StrategySide.BUY,
            order_type=StrategyOrderType.MARKET,
            size=1.0
        )
        assert order.size == 1.0

        # Multiple contracts
        order2 = OrderCommand(
            symbol="101V3000",
            side=StrategySide.SELL,
            order_type=StrategyOrderType.LIMIT,
            size=3.0,
            price=350.5
        )
        assert order2.size == 3.0

    def test_zero_size_rejected(self):
        """Order with size = 0 should be rejected"""
        with pytest.raises(ValueError, match="Order size must be at least 1 contract"):
            OrderCommand(
                symbol="101V3000",
                side=StrategySide.BUY,
                order_type=StrategyOrderType.MARKET,
                size=0
            )

    def test_negative_size_rejected(self):
        """Order with negative size should be rejected"""
        with pytest.raises(ValueError, match="Order size must be at least 1 contract"):
            OrderCommand(
                symbol="101V3000",
                side=StrategySide.SELL,
                order_type=StrategyOrderType.MARKET,
                size=-2.0
            )

    def test_fractional_size_below_one_rejected(self):
        """Order with size < 1 should be rejected"""
        with pytest.raises(ValueError, match="Order size must be at least 1 contract"):
            OrderCommand(
                symbol="101V3000",
                side=StrategySide.BUY,
                order_type=StrategyOrderType.MARKET,
                size=0.5
            )

    def test_order_to_dict_preserves_size(self):
        """Ensure OrderCommand.to_dict() preserves size correctly"""
        order = OrderCommand(
            symbol="101V3000",
            side=StrategySide.BUY,
            order_type=StrategyOrderType.MARKET,
            size=2.0,
            strategy_id="test_strategy",
            mode=TradingMode.MODE_B
        )

        order_dict = order.to_dict()
        assert order_dict["size"] == 2.0
        assert order_dict["symbol"] == "101V3000"
        assert order_dict["side"] == "BUY"
        assert order_dict["strategy_id"] == "test_strategy"


class TestStreamContractValidation:
    """Test ORDER_COMMAND_CONTRACT validation (already in test_architecture.py, but comprehensive here)"""

    def test_size_validator_accepts_valid_values(self):
        """Stream contract should accept size >= 1"""
        from src.common.stream_contracts import ORDER_COMMAND_CONTRACT, ValidationMode

        test_cases = [
            {"size": "1", "desc": "string '1'"},
            {"size": 1, "desc": "int 1"},
            {"size": 1.0, "desc": "float 1.0"},
            {"size": "5", "desc": "string '5'"},
            {"size": 10, "desc": "int 10"},
        ]

        for case in test_cases:
            order = {
                "symbol": "101V3000",
                "side": "BUY",
                "order_type": "MARKET",
                "size": case["size"],
                "strategy_id": "test",
                "timestamp": "1705312800.0",
            }

            is_valid, errors = ORDER_COMMAND_CONTRACT.validate(order, ValidationMode.STRICT)
            assert is_valid, f"Valid order rejected for {case['desc']}: {errors}"

    def test_size_validator_rejects_invalid_values(self):
        """Stream contract should reject size < 1"""
        from src.common.stream_contracts import ORDER_COMMAND_CONTRACT, ValidationMode

        invalid_cases = [
            {"size": "0", "desc": "string '0'"},
            {"size": 0, "desc": "int 0"},
            {"size": 0.0, "desc": "float 0.0"},
            {"size": "-1", "desc": "string '-1'"},
            {"size": -5, "desc": "int -5"},
            {"size": "0.5", "desc": "string '0.5'"},
            {"size": 0.99, "desc": "float 0.99"},
        ]

        for case in invalid_cases:
            order = {
                "symbol": "101V3000",
                "side": "BUY",
                "order_type": "MARKET",
                "size": case["size"],
                "strategy_id": "test",
                "timestamp": "1705312800.0",
            }

            is_valid, errors = ORDER_COMMAND_CONTRACT.validate(order, ValidationMode.WARN)
            assert not is_valid, f"Invalid order accepted for {case['desc']}"
            assert any("size" in err.lower() for err in errors), \
                f"Expected size validation error for {case['desc']}, got: {errors}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
