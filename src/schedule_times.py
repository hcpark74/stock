"""스케줄 시각 상수 — 단일 출처.

scheduler(잡 등록)·main(catchup)·api/server(UI 표시)가 공유한다.
apscheduler 의존이 없는 순수 모듈이므로 어디서든 안전하게 import 가능
(scheduler.py를 직접 import하면 apscheduler가 테스트 경로에 끌려온다).
"""

F1_H, F1_M = 9, 0
PAPER_FAST_PROBE_H, PAPER_FAST_PROBE_M, PAPER_FAST_PROBE_S = 8, 59, 45
BALANCE_PREFETCH_H, BALANCE_PREFETCH_M, BALANCE_PREFETCH_S = 8, 59, 50
F2_H, F2_M = 9, 10
F3_H, F3_M, F3_S = 9, 10, 10
F3_FILL_DEADLINE_H, F3_FILL_DEADLINE_M = 9, 11
# 청산은 KRX 연속매매 구간(~15:20) 안에서 끝나야 한다. 15:20부터는 장마감
# 동시호가라 시장가 매도가 즉시 체결되지 않고 15:30 단일가로 모이므로,
# F5의 체결 폴링·잔량 재주문(최대 3회)이 전부 미체결로 끝난다.
# 15:15 실행은 동시호가 시작까지 5분 여유를 남긴다.
F5_PRECHECK_H, F5_PRECHECK_M, F5_PRECHECK_S = 15, 14, 50
F5_EXEC_H, F5_EXEC_M, F5_EXEC_S = 15, 15, 0
