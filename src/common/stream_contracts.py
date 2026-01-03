"""
Stream Message Contracts (Schemas)

Defines explicit schemas for stream messages to prevent silent breaking changes
between producers and consumers.

Usage:
    from src.common.stream_contracts import validate_message, RAW_DATA_CONTRACT

    # Validate outgoing message
    validate_message(data, RAW_DATA_CONTRACT)

    # Or with decorator
    @validates_contract(RAW_DATA_CONTRACT)
    def publish(data):
        ...
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Union, Type, Tuple
from enum import Enum
from functools import wraps

logger = logging.getLogger(__name__)


class ValidationMode(Enum):
    """Contract validation modes"""
    DISABLED = "disabled"   # Production: no validation overhead
    WARN = "warn"           # Log warnings on mismatch
    STRICT = "strict"       # Raise exception on mismatch


@dataclass
class FieldSpec:
    """Specification for a single field"""
    name: str
    types: Tuple[type, ...]  # Allowed types
    required: bool = True
    description: str = ""
    validator: Optional[callable] = None  # Custom validation function

    def __post_init__(self):
        # Normalize single type to tuple
        if isinstance(self.types, type):
            self.types = (self.types,)

    def validate_value(self, value: Any) -> Tuple[bool, str]:
        """
        Validate a value against this field spec

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check type
        if not isinstance(value, self.types):
            # Allow string representations of numbers
            if str in self.types or (int, float) == self.types:
                try:
                    if float in self.types:
                        float(value)
                    elif int in self.types:
                        int(value)
                except (ValueError, TypeError):
                    return False, f"expected {self.types}, got {type(value).__name__}"
            else:
                return False, f"expected {self.types}, got {type(value).__name__}"

        # Custom validator
        if self.validator:
            try:
                if not self.validator(value):
                    return False, "custom validation failed"
            except Exception as e:
                return False, f"validator error: {e}"

        return True, ""


@dataclass
class StreamContract:
    """Contract definition for a stream's messages"""
    stream_name: str
    version: str
    fields: List[FieldSpec]
    description: str = ""
    allow_extra_fields: bool = True  # Allow fields not in spec

    def validate(
        self,
        data: Dict[str, Any],
        mode: ValidationMode = ValidationMode.WARN
    ) -> Tuple[bool, List[str]]:
        """
        Validate data against this contract

        Args:
            data: Message data to validate
            mode: Validation mode

        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        if mode == ValidationMode.DISABLED:
            return True, []

        errors = []

        # Check required fields
        for field_spec in self.fields:
            if field_spec.name not in data:
                if field_spec.required:
                    errors.append(f"missing required field: {field_spec.name}")
                continue

            value = data[field_spec.name]

            # None handling
            if value is None:
                if field_spec.required:
                    errors.append(f"null value for required field: {field_spec.name}")
                continue

            # Type/value validation
            is_valid, error = field_spec.validate_value(value)
            if not is_valid:
                errors.append(f"{field_spec.name}: {error}")

        # Check for unknown fields
        if not self.allow_extra_fields:
            known_fields = {f.name for f in self.fields}
            # Exclude internal fields
            internal_prefixes = ('_',)
            for key in data.keys():
                if not key.startswith(internal_prefixes) and key not in known_fields:
                    errors.append(f"unknown field: {key}")

        # Handle based on mode
        if errors:
            msg = f"Contract validation failed for {self.stream_name}: {errors}"
            if mode == ValidationMode.STRICT:
                raise ContractValidationError(msg, errors)
            else:
                logger.warning(msg)

        return len(errors) == 0, errors

    def get_schema_dict(self) -> Dict[str, Any]:
        """Get contract as a dictionary (for documentation)"""
        return {
            "stream": self.stream_name,
            "version": self.version,
            "description": self.description,
            "fields": [
                {
                    "name": f.name,
                    "types": [t.__name__ for t in f.types],
                    "required": f.required,
                    "description": f.description
                }
                for f in self.fields
            ]
        }


class ContractValidationError(Exception):
    """Raised when contract validation fails in strict mode"""
    def __init__(self, message: str, errors: List[str]):
        super().__init__(message)
        self.errors = errors


# ============================================================================
# Contract Definitions
# ============================================================================

RAW_DATA_CONTRACT = StreamContract(
    stream_name="RAW_DATA_STREAM",
    version="1.0",
    description="Raw tick/orderbook data from KIS WebSocket",
    fields=[
        FieldSpec("symbol", (str,), True, "Futures code (e.g., 101V3000)"),
        FieldSpec("timestamp", (str, float), True, "ISO timestamp or unix epoch"),
        FieldSpec("data_type", (str,), True, "Type: orderbook or trade"),
        FieldSpec("bid_price_1", (str, float, int), True, "Best bid price"),
        FieldSpec("ask_price_1", (str, float, int), True, "Best ask price"),
        FieldSpec("bid_qty_1", (str, float, int), True, "Best bid quantity"),
        FieldSpec("ask_qty_1", (str, float, int), True, "Best ask quantity"),
        FieldSpec("bid_price_2", (str, float, int), False, "2nd bid price"),
        FieldSpec("ask_price_2", (str, float, int), False, "2nd ask price"),
        FieldSpec("bid_qty_2", (str, float, int), False, "2nd bid quantity"),
        FieldSpec("ask_qty_2", (str, float, int), False, "2nd ask quantity"),
        FieldSpec("bid_price_3", (str, float, int), False, "3rd bid price"),
        FieldSpec("ask_price_3", (str, float, int), False, "3rd ask price"),
        FieldSpec("bid_price_4", (str, float, int), False, "4th bid price"),
        FieldSpec("ask_price_4", (str, float, int), False, "4th ask price"),
        FieldSpec("bid_price_5", (str, float, int), False, "5th bid price"),
        FieldSpec("ask_price_5", (str, float, int), False, "5th ask price"),
    ]
)

FEATURE_CONTRACT = StreamContract(
    stream_name="FEATURE_STREAM",
    version="1.0",
    description="Computed features for prediction and strategy",
    fields=[
        FieldSpec("symbol", (str,), True, "Futures code"),
        FieldSpec("timestamp", (str,), True, "ISO timestamp"),
        FieldSpec("ofi_z_score", (str, float), True, "Order Flow Imbalance Z-score"),
        FieldSpec("liquidity_score", (str, float), True, "Liquidity score (0-100)"),
        FieldSpec("spread", (str, float), False, "Bid-ask spread"),
        FieldSpec("mid_price", (str, float), False, "Mid price"),
        FieldSpec("rsi", (str, float), False, "RSI indicator"),
        FieldSpec("bb_position", (str, float), False, "Bollinger Band position"),
        FieldSpec("features", (list,), True, "Feature vector for ML model"),
        FieldSpec("candle", (dict,), False, "OHLCV candle data"),
    ]
)

PREDICTION_CONTRACT = StreamContract(
    stream_name="PREDICTION_STREAM",
    version="1.0",
    description="Model predictions for strategy decisions",
    fields=[
        FieldSpec("symbol", (str,), True, "Futures code"),
        FieldSpec("timestamp", (str,), True, "ISO timestamp"),
        FieldSpec("up_prob", (str, float), True, "Probability of price going up"),
        FieldSpec("down_prob", (str, float), True, "Probability of price going down"),
        FieldSpec("hold_prob", (str, float), False, "Probability of no change"),
        FieldSpec("confidence", (str, float), False, "Model confidence"),
        FieldSpec("model_version", (str,), False, "Model version used"),
    ]
)

ORDER_COMMAND_CONTRACT = StreamContract(
    stream_name="ORDER_COMMAND_STREAM",
    version="1.0",
    description="Order commands from strategy to executor",
    fields=[
        FieldSpec("symbol", (str,), True, "Futures code to trade"),
        FieldSpec("side", (str,), True, "BUY or SELL"),
        FieldSpec("order_type", (str,), True, "MARKET or LIMIT"),
        FieldSpec("size", (str, float, int), True, "Order quantity"),
        FieldSpec("price", (str, float, int), False, "Limit price (for LIMIT orders)"),
        FieldSpec("strategy_id", (str,), True, "Strategy that generated this order"),
        FieldSpec("mode", (str,), False, "Trading mode (A/B)"),
        FieldSpec("timestamp", (str, float), True, "Order generation time"),
    ]
)

# Contract registry
CONTRACTS: Dict[str, StreamContract] = {
    "RAW_DATA_STREAM": RAW_DATA_CONTRACT,
    "FEATURE_STREAM": FEATURE_CONTRACT,
    "PREDICTION_STREAM": PREDICTION_CONTRACT,
    "ORDER_COMMAND_STREAM": ORDER_COMMAND_CONTRACT,
}


# ============================================================================
# Validation Utilities
# ============================================================================

# Global validation mode (can be changed at runtime)
_validation_mode = ValidationMode.WARN


def set_validation_mode(mode: ValidationMode):
    """Set global validation mode"""
    global _validation_mode
    _validation_mode = mode
    logger.info(f"Contract validation mode set to: {mode.value}")


def get_validation_mode() -> ValidationMode:
    """Get current validation mode"""
    return _validation_mode


def validate_message(
    data: Dict[str, Any],
    contract: StreamContract,
    mode: ValidationMode = None
) -> bool:
    """
    Validate a message against a contract

    Args:
        data: Message data
        contract: Contract to validate against
        mode: Validation mode (uses global if not specified)

    Returns:
        True if valid
    """
    if mode is None:
        mode = _validation_mode
    is_valid, _ = contract.validate(data, mode)
    return is_valid


def get_contract(stream_name: str) -> Optional[StreamContract]:
    """Get contract for a stream"""
    return CONTRACTS.get(stream_name)


def validates_contract(contract: StreamContract):
    """
    Decorator to validate function output against a contract

    Usage:
        @validates_contract(RAW_DATA_CONTRACT)
        def create_message(tick):
            return {...}
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            if isinstance(result, dict):
                validate_message(result, contract)
            return result
        return wrapper
    return decorator


def document_contracts() -> str:
    """Generate markdown documentation for all contracts"""
    lines = ["# Stream Contracts\n"]

    for name, contract in CONTRACTS.items():
        lines.append(f"## {name}\n")
        lines.append(f"Version: {contract.version}\n")
        lines.append(f"{contract.description}\n")
        lines.append("\n| Field | Type | Required | Description |")
        lines.append("|-------|------|----------|-------------|")

        for f in contract.fields:
            types_str = " | ".join(t.__name__ for t in f.types)
            req = "Yes" if f.required else "No"
            lines.append(f"| {f.name} | {types_str} | {req} | {f.description} |")

        lines.append("\n")

    return "\n".join(lines)
