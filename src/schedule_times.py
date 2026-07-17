"""스케줄 시각 상수 — 단일 출처.

scheduler(잡 등록)·main(catchup)·api/server(UI 표시)가 공유한다.
apscheduler 의존이 없는 순수 모듈이므로 어디서든 안전하게 import 가능
(scheduler.py를 직접 import하면 apscheduler가 테스트 경로에 끌려온다).
"""

F1_H, F1_M = 9, 0
F2_H, F2_M = 9, 10
F3_H, F3_M, F3_S = 9, 10, 10
F3_FILL_DEADLINE_H, F3_FILL_DEADLINE_M = 9, 11
F5_PRECHECK_H, F5_PRECHECK_M, F5_PRECHECK_S = 10, 59, 50
F5_EXEC_H, F5_EXEC_M, F5_EXEC_S = 11, 0, 0
