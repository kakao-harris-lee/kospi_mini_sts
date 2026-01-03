"""
Redis 클라이언트 및 Stream 유틸리티

Features:
- Connection management (singleton)
- Stream publishing with distributed tracing
- Consumer groups with PEL handling
- Correlation ID propagation for debugging
"""
import redis
import json
import logging
import time
import uuid
import threading
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

import sys
from config.settings import settings

logger = logging.getLogger(__name__)


# Thread-local storage for correlation ID
_trace_context = threading.local()


def set_correlation_id(corr_id: str):
    """Set correlation ID for current thread"""
    _trace_context.correlation_id = corr_id


def get_correlation_id() -> str:
    """Get correlation ID for current thread (generates new if not set)"""
    if not hasattr(_trace_context, 'correlation_id') or _trace_context.correlation_id is None:
        _trace_context.correlation_id = str(uuid.uuid4())[:8]
    return _trace_context.correlation_id


def clear_correlation_id():
    """Clear correlation ID for current thread"""
    _trace_context.correlation_id = None


class RedisClient:
    """Redis 연결 관리 싱글톤"""
    _instance: Optional[redis.Redis] = None
    
    @classmethod
    def get_client(cls) -> redis.Redis:
        if cls._instance is None:
            cfg = settings.redis
            cls._instance = redis.Redis(
                host=cfg.host,
                port=cfg.port,
                password=cfg.password,
                db=cfg.db,
                decode_responses=cfg.decode_responses
            )
            # 연결 테스트
            cls._instance.ping()
            logger.info(f"Redis 연결 성공: {cfg.host}:{cfg.port}")
        return cls._instance
    
    @classmethod
    def close(cls):
        if cls._instance:
            cls._instance.close()
            cls._instance = None


@dataclass
class StreamMessage:
    """
    Stream 메시지 래퍼 with distributed tracing support

    Attributes:
        id: Redis message ID
        data: Parsed message data
        stream: Stream name
        correlation_id: Trace ID for distributed tracing
        parent_id: Parent message's correlation ID (for lineage)
        timestamp: Message creation timestamp
    """
    id: str
    data: Dict[str, Any]
    stream: str
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    parent_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_raw(cls, stream: str, msg_id: str, fields: Dict[str, str]) -> "StreamMessage":
        """Parse raw Redis message with tracing metadata"""
        parsed = {}

        # Extract tracing metadata
        correlation_id = fields.pop('_corr_id', None) or str(uuid.uuid4())[:8]
        parent_id = fields.pop('_parent_id', None) or None
        timestamp = float(fields.pop('_ts', 0)) or time.time()

        # JSON 필드 자동 파싱
        for k, v in fields.items():
            if k.endswith('_json'):
                try:
                    parsed[k.replace('_json', '')] = json.loads(v)
                except json.JSONDecodeError:
                    parsed[k] = v
            else:
                parsed[k] = v

        return cls(
            id=msg_id,
            data=parsed,
            stream=stream,
            correlation_id=correlation_id,
            parent_id=parent_id if parent_id else None,
            timestamp=timestamp
        )

    def create_child_id(self) -> str:
        """Create a new correlation ID that references this message as parent"""
        return f"{self.correlation_id}-{str(uuid.uuid4())[:4]}"

    def get_trace_dict(self) -> Dict[str, str]:
        """Get tracing metadata for publishing downstream"""
        return {
            '_corr_id': self.correlation_id,
            '_parent_id': self.parent_id or '',
            '_ts': str(self.timestamp)
        }

    def __repr__(self) -> str:
        return f"StreamMessage(id={self.id}, corr={self.correlation_id}, stream={self.stream})"


class StreamPublisher:
    """
    Stream에 데이터 발행 with distributed tracing support

    Usage:
        publisher = StreamPublisher("FEATURE_STREAM")

        # Simple publish (auto-generates correlation ID)
        publisher.publish({"symbol": "101V3000", ...})

        # Publish with parent tracing (for pipeline)
        publisher.publish(data, parent_message=input_msg)
    """

    def __init__(self, stream_name: str, maxlen: int = None):
        self.stream = stream_name
        self.maxlen = maxlen or settings.redis.stream_maxlen
        self.client = RedisClient.get_client()
        self._publish_count = 0

    def publish(
        self,
        data: Dict[str, Any],
        correlation_id: str = None,
        parent_message: StreamMessage = None
    ) -> str:
        """
        데이터를 Stream에 추가 with tracing metadata

        Args:
            data: Message data
            correlation_id: Override correlation ID (auto-generated if None)
            parent_message: Parent message for lineage tracking

        Returns:
            Redis message ID
        """
        processed = {}

        # Add tracing metadata
        if parent_message:
            # Inherit parent's correlation ID for lineage
            processed['_corr_id'] = parent_message.correlation_id
            processed['_parent_id'] = parent_message.id
        else:
            processed['_corr_id'] = correlation_id or get_correlation_id()
            processed['_parent_id'] = ''

        processed['_ts'] = str(time.time())

        # 딕셔너리/리스트는 JSON으로 변환
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                processed[f"{k}_json"] = json.dumps(v)
            else:
                processed[k] = str(v) if v is not None else ""

        msg_id = self.client.xadd(
            self.stream,
            processed,
            maxlen=self.maxlen
        )

        self._publish_count += 1
        logger.debug(
            f"Published to {self.stream}: {msg_id} "
            f"[corr={processed['_corr_id']}]"
        )
        return msg_id

    def get_stats(self) -> Dict[str, Any]:
        """Get publisher statistics"""
        return {
            "stream": self.stream,
            "publish_count": self._publish_count,
            "maxlen": self.maxlen
        }


class StreamConsumer(ABC):
    """
    Stream Consumer 베이스 클래스
    Consumer Group을 사용하여 안정적인 메시지 처리 보장
    """
    
    def __init__(
        self,
        stream_name: str,
        group_name: str,
        consumer_name: str = None
    ):
        self.stream = stream_name
        self.group = group_name
        self.consumer = consumer_name or f"{group_name}_worker_1"
        self.client = RedisClient.get_client()
        self.running = False
        
        self._ensure_group_exists()
    
    def _ensure_group_exists(self):
        """Consumer Group이 없으면 생성"""
        try:
            self.client.xgroup_create(
                self.stream, 
                self.group, 
                id='0',  # 처음부터 읽기
                mkstream=True  # Stream이 없으면 생성
            )
            logger.info(f"Consumer Group 생성: {self.group} on {self.stream}")
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.debug(f"Consumer Group 이미 존재: {self.group}")
            else:
                raise
    
    @abstractmethod
    def process_message(self, message: StreamMessage) -> bool:
        """
        메시지 처리 로직 (서브클래스에서 구현)
        Returns: 처리 성공 여부
        """
        pass
    
    def _read_pending(self) -> List[StreamMessage]:
        """미처리(Pending) 메시지 먼저 읽기"""
        messages = []
        pending = self.client.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: '0'},  # 0 = pending 메시지
            count=settings.consumer.read_count,
            block=None
        )
        
        if pending:
            for stream_name, stream_msgs in pending:
                for msg_id, fields in stream_msgs:
                    if fields:  # 빈 필드는 이미 ACK된 메시지
                        messages.append(
                            StreamMessage.from_raw(stream_name, msg_id, fields)
                        )
        return messages
    
    def _read_new(self) -> List[StreamMessage]:
        """새 메시지 읽기"""
        messages = []
        result = self.client.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: '>'},  # > = 새 메시지만
            count=settings.consumer.read_count,
            block=settings.consumer.block_ms
        )
        
        if result:
            for stream_name, stream_msgs in result:
                for msg_id, fields in stream_msgs:
                    messages.append(
                        StreamMessage.from_raw(stream_name, msg_id, fields)
                    )
        return messages
    
    def _ack(self, message: StreamMessage):
        """메시지 처리 완료 확인"""
        self.client.xack(self.stream, self.group, message.id)
        logger.debug(f"ACK: {message.id}")
    
    def run(self):
        """메인 처리 루프"""
        self.running = True
        logger.info(f"Consumer 시작: {self.consumer} [{self.stream}]")
        
        # 1. 먼저 미처리 메시지 처리
        pending = self._read_pending()
        if pending:
            logger.info(f"미처리 메시지 {len(pending)}개 발견, 처리 시작")
            for msg in pending:
                try:
                    if self.process_message(msg):
                        self._ack(msg)
                except Exception as e:
                    logger.error(f"메시지 처리 실패 (pending): {msg.id}, {e}")
        
        # 2. 새 메시지 계속 처리
        while self.running:
            try:
                messages = self._read_new()
                for msg in messages:
                    try:
                        if self.process_message(msg):
                            self._ack(msg)
                    except Exception as e:
                        logger.error(f"메시지 처리 실패: {msg.id}, {e}")
            except KeyboardInterrupt:
                logger.info("Consumer 종료 요청")
                self.stop()
            except Exception as e:
                logger.error(f"Consumer 오류: {e}")
    
    def stop(self):
        """Consumer 종료"""
        self.running = False
        logger.info(f"Consumer 종료: {self.consumer}")


class MultiStreamConsumer(ABC):
    """
    여러 Stream을 동시에 소비하는 Consumer
    Strategy Manager처럼 여러 소스를 조합해야 할 때 사용
    """
    
    def __init__(
        self,
        streams: Dict[str, str],  # {stream_name: group_name}
        consumer_name: str = None
    ):
        self.streams = streams
        self.consumer = consumer_name or "multi_consumer_1"
        self.client = RedisClient.get_client()
        self.running = False
        
        for stream, group in streams.items():
            self._ensure_group_exists(stream, group)
    
    def _ensure_group_exists(self, stream: str, group: str):
        try:
            self.client.xgroup_create(stream, group, id='0', mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
    
    @abstractmethod
    def process_messages(self, messages: Dict[str, List[StreamMessage]]) -> bool:
        """여러 Stream의 메시지를 함께 처리"""
        pass
    
    def run(self):
        self.running = True
        stream_ids = {s: '>' for s in self.streams.keys()}
        
        while self.running:
            try:
                result = self.client.xreadgroup(
                    list(self.streams.values())[0],  # 첫 번째 그룹 사용
                    self.consumer,
                    stream_ids,
                    count=settings.consumer.read_count,
                    block=settings.consumer.block_ms
                )
                
                if result:
                    grouped: Dict[str, List[StreamMessage]] = {}
                    for stream_name, stream_msgs in result:
                        grouped[stream_name] = [
                            StreamMessage.from_raw(stream_name, mid, fields)
                            for mid, fields in stream_msgs
                        ]
                    
                    if self.process_messages(grouped):
                        # ACK 처리
                        for stream_name, msgs in grouped.items():
                            for msg in msgs:
                                self.client.xack(
                                    stream_name,
                                    self.streams[stream_name],
                                    msg.id
                                )
            except KeyboardInterrupt:
                self.stop()
            except Exception as e:
                logger.error(f"MultiStreamConsumer 오류: {e}")
    
    def stop(self):
        self.running = False
