"""GitLab 웹훅을 받아 MR 리뷰를 실행하는 서버.

환경변수: WEBHOOK_SECRET(필수, GitLab 웹훅의 Secret token 과 같은 값), PORT(기본 8090),
         그 밖은 review_mr.py 와 같음. DRY_RUN=1 이면 게시하지 않고 로그로만 출력.

GitLab 은 웹훅 응답을 10초 안에 받아야 실패로 치지 않는다 — 리뷰는 수 분 걸리므로 즉시 202 로 답하고
큐에 넣는다. 작업자는 하나뿐이라 리뷰가 한 번에 하나씩 돈다(구독 한도를 한꺼번에 몰아 쓰지 않게).
"""
import hmac
import json
import os
import queue
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import review_mr

SECRET = os.environ["WEBHOOK_SECRET"]
DRY_RUN = os.environ.get("DRY_RUN") == "1"
jobs: "queue.Queue[int]" = queue.Queue()


def should_review(event):
    """리뷰할 이벤트인가. 열림·다시 열림, 또는 새 커밋이 푸시된 update 만. Draft·develop 외 대상은 제외."""
    if event.get("object_kind") != "merge_request":
        return False
    a = event.get("object_attributes") or {}
    if a.get("draft") or a.get("work_in_progress") or a.get("target_branch") != "develop":
        return False
    action = a.get("action")
    # update 는 제목·설명만 바꿔도 온다 — oldrev 가 있어야 새 커밋이 들어온 것
    return action in ("open", "reopen") or (action == "update" and bool(a.get("oldrev")))


def worker():
    while True:
        iid = jobs.get()
        try:
            result = review_mr.review(iid, do_post=not DRY_RUN)
            print(f"[review] MR !{iid}: " + ("게시" if result is True else "이미 리뷰함" if result is False
                                             else f"dry-run\n{result}"), flush=True)
        except Exception:  # 한 MR 의 실패가 서버를 멈추지 않게
            print(f"[review] MR !{iid} 실패\n{traceback.format_exc()}", flush=True)
        finally:
            jobs.task_done()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # nginx·모니터링용 생존 확인
        self._reply(200 if self.path.endswith("/health") else 404, {"queued": jobs.qsize()})

    def do_POST(self):
        if not self.path.endswith("/hook"):
            return self._reply(404, {})
        if not hmac.compare_digest(self.headers.get("X-Gitlab-Token", ""), SECRET):
            return self._reply(401, {})
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 5 * 1024 * 1024)
            event = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return self._reply(400, {})
        if should_review(event):
            iid = event["object_attributes"]["iid"]
            jobs.put(iid)
            return self._reply(202, {"queued": iid})
        self._reply(200, {"skipped": True})

    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 요청 로그에 헤더(토큰)가 찍히지 않게 경로·코드만
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", "8090"))
    print(f"[review-bot] listening :{port} dry_run={DRY_RUN}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
