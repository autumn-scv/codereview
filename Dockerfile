# S15P31 리뷰봇 — GitLab MR 을 폴링해 Codex(ChatGPT 구독) 로 리뷰, 실패하면 Gemini(무료)
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @openai/codex@0.164.0 \
    && rm -rf /var/lib/apt/lists/* /root/.npm
RUN pip install --no-cache-dir requests

WORKDIR /app
COPY review_mr.py poll.py server.py prompt.md verify.md ./

# 루트로 돌리지 않는다. Codex 로그인(auth.json)은 /codex 볼륨 — 토큰 갱신을 위해 쓰기 가능해야 한다
USER nobody
ENV PYTHONUNBUFFERED=1 CODEX_HOME=/codex
# 기본은 폴링 모드(공개 주소·웹훅 불필요). 웹훅으로 바꾸려면 CMD 를 server.py 로 덮어쓰고 8090 을 nginx 뒤에 둔다
CMD ["python3", "poll.py"]
