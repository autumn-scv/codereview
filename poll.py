"""폴링 모드 — 웹훅 없이 봇이 주기적으로 GitLab 에 새 MR·새 커밋이 있는지 묻는다.

공개 주소·nginx·웹훅 등록이 필요 없다(밖에서 들어오는 길이 없음). 반응은 최대 POLL_SEC 만큼 늦다.

환경변수: review_mr.py 와 같음 + POLL_SEC(기본 60), SKIP_EXISTING(1 이면 켤 때 열려 있던 MR 은 다음 커밋부터 리뷰)
"""
import hmac
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import review_mr

POLL_SEC = int(os.environ.get("POLL_SEC", "60"))


def open_mrs():
    """develop 으로 가는 열린 MR 중 Draft 가 아닌 것: {iid: 최신 커밋 SHA}"""
    mrs = review_mr.gl_all("/merge_requests", state="opened", target_branch="develop", wip="no")
    return {m["iid"]: m["sha"] for m in mrs}


WORKERS = int(os.environ.get("REVIEW_WORKERS", "3"))  # 동시에 리뷰하는 MR 수 — 한 MR 이 다른 MR 을 줄 세우지 않게
WAKE = threading.Event()  # 웹훅이 오면 폴링 대기를 끊고 바로 MR 목록을 다시 본다


class Hook(BaseHTTPRequestHandler):
    """GitLab MR 웹훅 수신(nginx /review-bot/hook → 8090). 내용은 쓰지 않고 '지금 확인해'라는 신호로만 쓴다 —
    무엇을 리뷰할지는 폴링과 같은 경로(GitLab API)로 정하므로 웹훅 본문을 믿을 필요가 없다."""

    def do_POST(self):
        token = self.headers.get("X-Gitlab-Token") or ""
        if not hmac.compare_digest(token, os.environ.get("WEBHOOK_SECRET", "")):
            self.send_response(401)
            self.end_headers()
            return
        self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1_000_000))
        self.send_response(200)  # GitLab 은 10초 안에 응답을 못 받으면 실패로 친다 — 바로 응답하고 깨우기만 한다
        self.end_headers()
        WAKE.set()

    def log_message(self, *args):  # 요청 로그에 헤더(토큰)가 찍히지 않게 끈다
        pass


def start_hook_server():
    if not os.environ.get("WEBHOOK_SECRET"):
        return  # 비밀값이 없으면 웹훅을 받지 않는다(폴링만)
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("HOOK_PORT", "8090"))), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("[hook] 웹훅 수신 대기: 8090", flush=True)


def review_one(iid, sha):
    """스레드에서 MR 하나를 리뷰한다. 처리 끝났으면 True(이 SHA 는 다시 안 봄), 다음 주기에 다시 봐야 하면 False."""
    try:
        result = review_mr.review(iid, do_post=True)  # 같은 커밋 이름표가 있으면 알아서 건너뜀
        print(f"[poll] MR !{iid} {sha[:8]}: " + ("게시" if result is True else "이미 리뷰함"), flush=True)
        return True
    except review_mr.NotReady:  # Jenkins 테스트가 끝나면 다음 주기에 리뷰 — 실패가 아니므로 로그를 남기지 않는다
        return False
    except Exception:  # 한 MR 실패가 다른 MR 리뷰를 막지 않게. 다음 주기에 재시도
        print(f"[poll] MR !{iid} 실패\n{traceback.format_exc()}", flush=True)
        return False


def main():
    # 켤 때 열려 있던 MR 도 리뷰한다 — 이미 리뷰한 커밋은 review() 가 코멘트 이름표로 걸러내므로 중복되지 않는다.
    # (열린 MR 이 아주 많아 한도를 몰아 쓸까 걱정되면 SKIP_EXISTING=1 로 켜기 전 MR 을 건너뛸 수 있다)
    seen = open_mrs() if os.environ.get("SKIP_EXISTING") == "1" else {}
    running = {}  # iid → (sha, future) — 같은 MR 을 두 스레드가 동시에 리뷰하지 않게
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    start_hook_server()
    print(f"[poll] 시작: {POLL_SEC}초 간격, 동시 {WORKERS}개" + (f", 기존 열린 MR {len(seen)}개 건너뜀" if seen else ""), flush=True)
    while True:
        for iid, (sha, fut) in list(running.items()):  # 끝난 리뷰 정리
            if fut.done():
                del running[iid]
                if fut.result():
                    seen[iid] = sha
        try:
            current = open_mrs()
        except Exception:  # GitLab 일시 장애 — 멈추지 않고 다음 주기에 다시
            print(f"[poll] MR 목록 조회 실패\n{traceback.format_exc()}", flush=True)
            current = {}
        for iid, sha in current.items():
            if seen.get(iid) == sha or iid in running:
                continue  # 이미 처리했거나 지금 리뷰 중(새 커밋이면 끝난 뒤 다음 주기에 다시 본다)
            running[iid] = (sha, pool.submit(review_one, iid, sha))
        if WAKE.wait(POLL_SEC):  # 웹훅이 오면 바로 깨어난다. 안 오면 POLL_SEC 마다(Jenkins 완료·놓친 이벤트 확인)
            WAKE.clear()
            time.sleep(3)  # 푸시 직후엔 MR 의 최신 커밋이 API 에 반영되기까지 잠깐 걸린다


if __name__ == "__main__":
    main()
