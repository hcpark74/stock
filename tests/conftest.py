"""테스트 공통 설정."""
import logging

# 테스트 중 stock 로거 stdout/파일 출력 억제
_stock_log = logging.getLogger("stock")
_stock_log.handlers = []
_stock_log.propagate = False
