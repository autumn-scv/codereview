# S15P31 리뷰봇 — 설치 안내

MR 이 `develop` 으로 올라오면 **Codex(내 ChatGPT 구독 한도)** 가 저장소 스냅샷을 읽으며 검토하고(실패·한도 초과 시 **Gemini 무료 등급**이 대신), 팀 리뷰 규칙(`[필수]/[제안]/[질문]`)대로 **MR 코멘트 1개**를 단다. 새 커밋을 푸시하면 같은 코멘트를 갱신한다.

- **폴링 방식**: 봇이 60초마다 GitLab 에 새 MR·새 커밋을 묻는다. 공개 주소·nginx 경로·GitLab 웹훅 등록이 **필요 없다**.
- Draft MR, `develop` 이 아닌 대상(릴리즈 MR)은 건너뛴다.
- 컨테이너를 켤 때 열려 있던 MR 도 리뷰한다. 이미 리뷰한 커밋은 코멘트 이름표로 건너뛴다(재시작해도 중복 리뷰 없음).

## 준비물 (토큰 2개 — 둘 다 저장소·대화방에 올리지 말 것)

| 이름 | 만드는 곳 | 설명 |
|---|---|---|
| `GITLAB_TOKEN` | S15P31E106 → Settings → Access tokens → **Project access token**, 역할 `Reporter`, scope `api`, 만료일 지정 | 코멘트가 봇 이름으로 달린다 |
| `GEMINI_API_KEY` | aistudio.google.com → Get API key. **결제 수단을 연결하지 않은** 프로젝트에서 만들 것 | 무료 등급이라 청구되지 않는다. 대신 보낸 코드가 Google 제품 개선에 쓰일 수 있다 |
| Codex 로그인 `auth.json` | 내 PC `~/.codex/auth.json` (ChatGPT 로그인) | `.env` 가 아니라 `~/review-bot/codex/` 폴더에 둔다. **API 키 로그인이면 봇이 Codex 를 쓰지 않는다**(과금 차단) |

> **과금 차단**: 봇은 ① `auth_mode=chatgpt` 일 때만 Codex 실행 ② `OPENAI_API_KEY`·`CODEX_API_KEY` 를 Codex 에 넘기지 않음 ③ `forced_login_method="chatgpt"`.
> **추가 크레딧**은 코드로 막을 수 없다 — ChatGPT 설정에서 **크레딧 구매·자동 충전을 꺼 둘 것**. 그러면 한도 초과 시 Codex 가 실패하고 Gemini 로 넘어간다.
> **주의**: 서버에 접근 가능한 사람은 이 `auth.json` 으로 내 ChatGPT 계정을 쓸 수 있다. PC 와 같은 파일을 쓰면 한쪽 토큰 갱신 때 다른 쪽 로그인이 풀릴 수 있다(그러면 PC 에서 `codex login` 다시).

## 설치 (EC2)

```bash
# 1. 이 폴더를 서버로 복사 (예: ~/review-bot)
scp -i <키파일>.pem -r 코드리뷰봇 <사용자>@<서버주소>:~/review-bot

# 2. 서버에서 비밀값 파일 작성 — 본인만 읽을 수 있게
cd ~/review-bot
cat > .env <<'EOF'
GITLAB_TOKEN=<Project access token>
GEMINI_API_KEY=<AI Studio 키>
POLL_SEC=60
EOF
chmod 600 .env

# 2-1. Codex 로그인 파일 — 내 PC 에서 복사 후 컨테이너 사용자(nobody)만 읽고 쓰게
#   (PC) scp -i <키파일>.pem ~/.codex/auth.json <사용자>@<서버주소>:~/review-bot/codex/auth.json
mkdir -p codex && sudo chown -R 65534:65534 codex && sudo chmod 700 codex

# 3. 빌드·실행 (팀 docker-compose 와 별개의 컨테이너 하나)
docker build -t s15p31-review-bot:codex .
# Codex 샌드박스(bwrap)용: 서버 커널의 사용자 네임스페이스 제한을 푼다(재부팅 후에도 유지)
echo 'kernel.apparmor_restrict_unprivileged_userns=0' | sudo tee /etc/sysctl.d/60-codex-userns.conf
sudo sysctl -qw kernel.apparmor_restrict_unprivileged_userns=0
# seccomp·apparmor 해제는 컨테이너 안에서 bwrap 이 네임스페이스를 만들기 위함 — 실제 격리는 Codex 샌드박스(읽기 전용·네트워크 차단)가 한다
docker run -d --name review-bot --restart unless-stopped \
  --env-file .env -v ~/review-bot/codex:/codex --memory 3g --cpus 2 \
  --network tingle_default \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  s15p31-review-bot:codex

# 4. 동작 확인 — "[poll] 시작: 60초 간격 …" 이 보이면 정상
docker logs -f review-bot
```

## 운영

| 할 일 | 명령 |
|---|---|
| 로그 보기 | `docker logs -f review-bot` |
| 멈추기 / 다시 켜기 | `docker stop review-bot` / `docker start review-bot` |
| 코드·프롬프트 수정 후 반영 | `docker build -t s15p31-review-bot:codex . && docker rm -f review-bot` → 3번의 `docker run` 다시 |
| 완전히 제거 | `docker rm -f review-bot && docker rmi s15p31-review-bot:codex` |

## 동작 원리 (요약)

1. 열린 MR 의 최신 커밋 저장소 스냅샷을 임시 폴더에 내려받아 변경 파일의 전체 내용을 읽는다(임시 폴더는 바로 삭제).
2. MR 정보 + diff + 변경 파일 전체를 `codex exec --sandbox read-only` 에 넘긴다. Codex 는 스냅샷 폴더 안의 호출부 등 다른 파일도 읽는다. Codex 가 실패하면(한도 초과·로그인 만료·타임아웃) 같은 내용을 Gemini 에 보낸다(Gemini 는 변경 파일만 본다). 코멘트 끝에 어느 엔진이 검토했는지 적힌다.
3. 결과에서 토큰 모양 문자열을 지우고, 컨벤션 검사 결과와 합쳐 코멘트로 단다.

## 알아둘 점

- 모델은 기본 `gemini-3.6-flash`(`REVIEW_MODEL` 로 변경). 리뷰마다 `docker logs` 에 `[usage]` 줄로 토큰 수가 남는다.
- 무료 한도 초과(429)·요청 몰림(503)·네트워크 오류는 코멘트를 달지 않고 60초 뒤 다시 시도한다. 청구는 없다.
- 웹훅 방식으로 바꾸려면 `server.py`(포트 8090)를 실행하고 nginx 경로와 GitLab 웹훅(`/review-bot/hook`, Secret token)을 등록한다.

## 2026-11-03 교체 기록

- 옛 Gemini 봇 → Codex 우선 봇으로 교체. 되돌리기용으로 서버에 남겨 둔 것: 폴더 `~/review-bot.bak-20261103`, 이미지 `s15p31-review-bot:gemini-20261103`
- 되돌리기: `docker rm -f review-bot && docker run -d --name review-bot --restart unless-stopped --env-file ~/review-bot/.env --memory 3g --cpus 2 s15p31-review-bot:gemini-20261103`
- **Codex 샌드박스**: Docker 기본 seccomp 와 Ubuntu 의 `apparmor_restrict_unprivileged_userns=1` 이 `bwrap` 을 막아 Codex 가 변경 파일 밖을 못 읽었다. 3번처럼 서버 제한을 0으로, 컨테이너 seccomp·apparmor 를 해제해 해결(시험: 읽기 가능·쓰기 차단·네트워크 차단). 샌드박스를 끄는(`danger-full-access`) 방법은 쓰지 않는다 — MR 코드 속 지시로 `auth.json` 이 밖으로 나갈 수 있다.
- 봇을 내릴 때 서버 제한 원복: `sudo rm /etc/sysctl.d/60-codex-userns.conf && sudo sysctl -qw kernel.apparmor_restrict_unprivileged_userns=1`
