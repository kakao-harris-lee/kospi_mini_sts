
# Real-time Trading Skeleton (Redis Streams)

This is a template project demonstrating a real-time trading pipeline using:
- KoreaInvestment Adapter (OAuth + WebSocket REST stubs)
- Feed Handler (WebSocket -> Redis Streams)
- Strategy Engine (Redis Streams consumer -> generates signals)
- Order Executor (consumes signals -> sends orders)
- Position Manager (simple stateful consumer)
- Redis Streams as the central event bus

NOTE: Broker endpoints are stubs/templates. Replace with real API paths and credentials.
