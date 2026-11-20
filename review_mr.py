"""S15P31E106 MR 리뷰봇 — Codex(ChatGPT 구독 로그인) 우선, 실패하면 Gemini(무료 등급).

사용:
    python review_mr.py <MR번호>            # dry-run: 결과를 화면에만 출력 (기본)
    python review_mr.py <MR번호> --post     # MR 에 코멘트 게시(이미 있으면 갱신)
    python review_mr.py <MR번호> --post --force  # 같은 커밋이어도 다시 리뷰해 코멘트 갱신

환경변수:
    GITLAB_TOKEN              필수. api scope (Project Access Token 권장 — 코멘트가 봇 이름으로 달림)
    GEMINI_API_KEY            Codex 실패 시 대체. Google AI Studio 키 — 결제 수단을 연결하지 않은 프로젝트면 무료 등급이라 청구되지 않는다
    REVIEW_MODEL              선택. Gemini 모델, 기본 gemini-3.6-flash
    CODEX_HOME                선택. Codex 로그인 폴더(auth.json). 기본 ~/.codex — ChatGPT 로그인이 아니면 Codex 를 쓰지 않는다
    CODEX_MODEL               선택. 평소 Codex 모델(기본 gpt-6.1-sol)
    CODEX_LIGHT_MODEL         선택. 한도가 빨리 차는 날 모델(기본 gpt-6-luna)
    CODEX_LIGHT_AT            선택. 주간 한도 사용률(%)이 이 이상이면 LIGHT 모델(기본 65)
    RELATED_MAX_CHARS         선택. 관련 파일(호출부·테스트)에 쓰는 입력 상한(기본 120000자)
    REVIEW_MAX_CHARS          선택. 모델에 보내는 내용 상한(기본 400000자 ≈ 10만 토큰) — 무료 등급 분당 토큰 한도 대비

두 겹으로 리뷰한다.
  1) 컨벤션 검사 — convention/gitlab-convention.md 규칙을 정규식으로 판정(LLM 없음, 결정적)
  2) 코드 리뷰   — diff + 변경 파일의 커밋 시점 전체 내용으로 리뷰.
                   Codex 는 스냅샷 폴더 안에서 읽기 전용으로 돌아 호출부 등 다른 파일도 읽는다.
                   Codex 가 한도 초과 등으로 실패하면 Gemini 가 대신한다(Gemini 는 변경 파일만 본다).
"""
import datetime
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.parse

import subprocess

import requests

GITLAB = "https://lab.ssafy.com/api/v4"
PROJECT = urllib.parse.quote("s15-final/S15P31E106", safe="")
WEB = "https://lab.ssafy.com/s15-final/S15P31E106"  # 코멘트의 파일 링크용
MARK = "<!-- s15p31-review-bot:sha="
HERE = os.path.dirname(os.path.abspath(__file__))

# ── 컨벤션 규칙 (gitlab-convention.md) ──────────────────────────
BRANCH_RE = re.compile(r"^(ai|be|fe|infra)-(feature|fix|refactor|docs|chore)/[a-z0-9]+(-[a-z0-9]+)*-[A-Z][A-Z0-9]+-\d+$")
MR_TITLE_RE = re.compile(r"^\[(AI|BE|FE|INFRA)\] .+ [A-Z][A-Z0-9]+-\d+$")
COMMIT_RE = re.compile(r"^(feat|fix|refactor|docs|test|chore): \S.*$")
ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
FORBIDDEN = [  # (규칙 ID, 판정 함수, 설명)
    ("no-agent-files", lambda p: os.path.basename(p) in ("AGENTS.md", "CLAUDE.md") or "/.claude/" in "/" + p,
     "AI 에이전트 지침 파일 커밋 금지"),
    ("no-large-artifacts", lambda p: os.path.basename(p).startswith(".env")
     or p.endswith((".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf", ".parquet")),
     "모델 가중치·데이터셋·.env 커밋 금지"),
]
SPECIAL_BRANCHES = ("develop", "master")  # develop→master 같은 릴리즈 MR 은 이름 규칙 대상이 아니다

# 게시 전에 지우는 비밀값 모양 — MR 코드에 숨긴 지시로 에이전트가 토큰을 출력하게 만드는 공격 대비(최후 방어선)
SECRET_PATTERNS = [re.compile(r"glpat-[\w-]{20,}"), re.compile(r"sk-ant-[\w-]{20,}"), re.compile(r"AIza[\w-]{30,}"),
                   re.compile(r"sk-[\w-]{20,}"), re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")]  # OpenAI 키, JWT(Codex 토큰)


def gl_get(path, **params):
    r = requests.get(f"{GITLAB}/projects/{PROJECT}{path}",
                     headers={"PRIVATE-TOKEN": os.environ["GITLAB_TOKEN"]}, params=params, timeout=60)
    r.raise_for_status()
    return r


def gl_all(path, **params):
    """페이지를 끝까지 넘기며 목록을 모은다 — 안 넘기면 101번째부터 조용히 누락된다."""
    out, page = [], 1
    while True:
        r = gl_get(path, per_page=100, page=page, **params)
        out += r.json()
        if not r.headers.get("X-Next-Page"):
            return out
        page += 1


# ── 1) 컨벤션 검사 ─────────────────────────────────────────────
def check_conventions(mr, commits, diffs):
    """[(심각도, 규칙 ID, 한 줄 설명)] — 같은 규칙 위반이 여러 커밋에 있으면 한 줄로 묶는다."""
    findings = []
    src = mr["source_branch"]
    if src not in SPECIAL_BRANCHES and not BRANCH_RE.match(src):
        findings.append(("필수", "branch-name", f"브랜치 `{src}` 가 `{{파트}}-{{타입}}/요약-KEY-번호` 형식 아님. 예: `be-fix/secret-leak-KEY-41`"))
    if mr["target_branch"] != "develop" and src != "develop":
        findings.append(("필수", "branch-model", f"작업 브랜치는 `develop` 으로 MR 올려야 함 (현재 대상: `{mr['target_branch']}`)"))
    if src not in SPECIAL_BRANCHES and not MR_TITLE_RE.match(mr["title"]):
        findings.append(("필수", "mr-title", "MR 제목이 `[파트] 요약 KEY-번호` 형식 아님. 예: `[FE] 대화창 스트리밍 표시 KEY-23`"))

    bad = {"형식": [], "마침표": [], "Jira 키": []}
    for c in commits:
        subject = c["title"]
        if subject.startswith("Merge "):
            continue  # 머지 커밋은 규칙 대상 아님
        if not COMMIT_RE.match(subject):
            bad["형식"].append(c["short_id"])
        elif subject.rstrip().endswith("."):
            bad["마침표"].append(c["short_id"])
        elif ISSUE_KEY_RE.search(subject):
            bad["Jira 키"].append(c["short_id"])
    templates = {"형식": "커밋 {n}개({who})가 `타입: 요약` 형식 아님. 예: `feat: 토큰 저장소 추가`",
                 "마침표": "커밋 {n}개({who})의 요약 끝에 마침표 있음",
                 "Jira 키": "커밋 {n}개({who})에 Jira 키 있음. 키는 MR 제목에만 씀"}
    for kind, shas in bad.items():
        if shas:
            who = f"`{shas[0]}`" + (f" 외 {len(shas) - 1}개" if len(shas) > 1 else "")
            findings.append(("필수", "commit-format", templates[kind].format(n=len(shas), who=who)))

    for d in diffs:
        if d["deleted_file"]:
            continue
        for rule, hit, desc in FORBIDDEN:
            if hit(d["new_path"]):
                findings.append(("필수", rule, f"`{d['new_path']}` — {desc}"))
    desc = mr.get("description") or ""
    if "## 체크리스트" not in desc:
        findings.append(("제안", "mr-template", "MR 본문 템플릿(이슈·변경 내용·확인 방법·체크리스트) 비어 있음"))
    elif "- [ ]" in desc:
        findings.append(("질문", "mr-template", "체크리스트에 체크 안 된 항목 있음"))
    return findings


# ── 2) 코드 리뷰 (Codex → Gemini) ──────────────────────────────────────
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def extract_snapshot(mr):
    """MR 소스 커밋의 저장소 스냅샷을 임시 폴더에 푼다 — 변경 파일의 전체 내용을 읽으려고."""
    ws = tempfile.mkdtemp(prefix=f"review-mr{mr['iid']}-")
    archive = gl_get("/repository/archive.tar.gz", sha=mr["sha"]).content
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        # 압축 안의 ../ 경로·절대경로·링크로 폴더 밖에 파일을 쓰는 공격 차단
        if hasattr(tarfile, "data_filter"):
            tar.extractall(ws, filter="data")
        else:  # filter 인자가 없는 구버전 Python 대비 — 같은 검사를 직접 한다
            for m in tar.getmembers():
                name = m.name.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/") or m.issym() or m.islnk() or m.isdev():
                    raise ValueError(f"안전하지 않은 아카이브 항목: {m.name}")
            tar.extractall(ws)
    return ws, os.path.join(ws, os.listdir(ws)[0])  # GitLab 아카이브는 최상위 폴더 하나로 감싸져 있다


def build_input(mr, diffs, root):
    """모델에 보낼 본문: MR 정보 → diff → 변경 파일 전체. 상한을 넘는 파일은 빼고 그 사실을 적는다."""
    limit = int(os.environ.get("REVIEW_MAX_CHARS", "400000"))
    parts = [f"# MR !{mr['iid']}: {mr['title']}",
             f"- 소스 브랜치: `{mr['source_branch']}` → 대상: `{mr['target_branch']}`",
             "\n## 작성자 설명 (참고용 — 근거로 삼지 말 것)\n", mr.get("description") or "(없음)",
             "\n## 변경 내용 (diff)\n"]
    parts += [f"--- a/{d['old_path']}\n+++ b/{d['new_path']}\n{d['diff']}" for d in diffs]
    parts.append("\n## 변경 파일 전체 내용 (커밋 시점)\n")
    used, skipped = sum(map(len, parts)), []
    for d in diffs:
        if d["deleted_file"]:
            continue
        try:
            with open(os.path.join(root, d["new_path"]), encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):  # 바이너리·이미지 등
            continue
        if used + len(text) > limit:
            skipped.append(d["new_path"])
            continue
        used += len(text)
        parts.append(f"### {d['new_path']}\n```\n{text}\n```")
    if skipped:
        parts.append("\n(용량 상한으로 전체 내용을 싣지 못한 파일 — diff 로만 판단할 것: " + ", ".join(skipped) + ")")
    parts += related_files(diffs, root, min(limit - used, int(os.environ.get("RELATED_MAX_CHARS", "120000"))))
    return "\n".join(parts), skipped


SOURCE_EXT = (".java", ".kt", ".kts", ".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".sql", ".yml", ".yaml", ".gradle")
SKIP_DIRS = {".git", "node_modules", "build", "dist", ".next", ".gradle", "__pycache__", ".venv", "venv", "target", ".idea"}
GENERIC_NAMES = {"main", "index", "app", "utils", "util", "config", "types", "constants", "test", "tests", "setup",
                 "init", "__init__", "conftest", "readme", "application", "settings", "common", "base", "api"}


def related_files(diffs, root, budget):
    """바뀌지 않았지만 바뀐 코드를 이름으로 참조하는 파일(호출부·테스트·계약 상대)을 골라 싣는다.
    서버 컨테이너에서는 Codex 샌드박스가 막혀 저장소를 직접 읽지 못하므로, 봇이 미리 찾아 넘긴다.
    ponytail: 파일 이름(클래스·모듈명) 단어 일치로만 찾는다 — 메서드 단위 호출 추적이 필요해지면 그때 심볼 인덱스로"""
    changed = {d["new_path"] for d in diffs}
    keys = {os.path.splitext(os.path.basename(d["new_path"]))[0] for d in diffs if not d["deleted_file"]}
    keys = {k for k in keys if len(k) >= 4 and k.lower() not in GENERIC_NAMES}
    # 반대 방향: 바뀐 파일이 불러 쓰는 파일(import·타입 참조) — 파일 이름이 바뀐 코드 안에 단어로 나오면 의존 대상
    words = set()
    for p in changed:
        try:
            with open(os.path.join(root, p), encoding="utf-8") as f:
                words |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", f.read()))
        except (OSError, UnicodeDecodeError):
            pass
    words = {w for w in words if w.lower() not in GENERIC_NAMES}
    if budget <= 0 or not (keys or words):
        return []
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(keys))) + r")\b") if keys else None
    hits = []  # (참조한 이름 수, 크기, 경로, 내용)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(SOURCE_EXT):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in changed or os.path.getsize(full) > 60000:  # 너무 큰 파일은 한 개가 예산을 다 먹는다
                continue
            try:
                with open(full, encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            found = set(pattern.findall(text)) if pattern else set()
            used_by_change = os.path.splitext(name)[0] in words  # 바뀐 코드가 이 파일을 불러 씀
            if found or used_by_change:
                hits.append((len(found) + 2 * used_by_change, len(text), rel, text))  # 직접 의존을 더 우선
    hits.sort(key=lambda h: (-h[0], h[1]))  # 바뀐 이름을 많이 참조할수록, 같으면 작은 파일부터
    out, used, left = [], 0, []
    for _, size, rel, text in hits:
        if used + size > budget:
            left.append(rel)
            continue
        used += size
        out.append(f"### {rel}\n```\n{text}\n```")
    if not out:
        return []
    head = ["\n## 관련 파일 (바뀌지 않았지만 바뀐 클래스·모듈 이름을 참조하는 파일 — 호출부·테스트·계약 상대)\n"]
    tail = [f"\n(용량 상한으로 싣지 못한 관련 파일 {len(left)}개: " + ", ".join(left[:30]) + ")"] if left else []
    print(f"[input] 관련 파일 {len(out)}개 ({used:,}자), 제외 {len(left)}개", flush=True)
    return head + out + tail


class NotReady(Exception):
    """아직 리뷰할 때가 아님(Jenkins 테스트 진행 중) — 폴러가 다음 주기에 다시 본다. 실패가 아니다."""


def gemini_ask(system, text):
    model = os.environ.get("REVIEW_MODEL", "gemini-3.6-flash")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    # 네트워크 오류·429(무료 한도)·5xx(요청 몰림)는 일시적 — 예외로 올려 코멘트를 달지 않고, 폴러가 다음 주기에 다시 시도한다.
    # ponytail: 하루 한도가 찬 날엔 60초마다 헛요청한다(무료라 비용 없음). 거슬리면 429 때 다음 날까지 쉬게 할 것
    r = requests.post(GEMINI_URL.format(model=model), headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"].strip()},
                      json=body, timeout=180)
    if r.status_code == 429 or r.status_code >= 500:
        raise RuntimeError(f"gemini 일시 실패 {r.status_code}: {r.text[-300:]}")
    if r.status_code != 200:  # 400 등 다시 해도 안 될 오류 — 원문은 서버 로그에만
        print(f"[review] gemini 실패 {r.status_code}: {r.text[-500:]}", file=sys.stderr)
        return ""
    data = r.json()
    u = data.get("usageMetadata") or {}
    print(f"[usage] engine=gemini model={model} tokens={u.get('totalTokenCount', 0):,}", flush=True)
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError):  # 안전 필터 차단 등으로 답이 비었을 때
        print(f"[review] gemini 빈 응답: {str(data)[-500:]}", file=sys.stderr)
        return ""


CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-6.1-sol")          # 평소: 결함을 더 잘 잡는 큰 모델(속도는 추론 강도 medium 으로 확보)
CODEX_LIGHT_MODEL = os.environ.get("CODEX_LIGHT_MODEL", "gpt-6-luna")  # 한도가 빨리 차는 날: 작은 모델
CODEX_LIGHT_AT = float(os.environ.get("CODEX_LIGHT_AT", "65"))        # 주간 한도 사용률(%)이 이 이상이면 작은 모델로
WEEK_MINUTES = 7 * 24 * 60
USAGE_FILE = os.path.join(CODEX_HOME, "bot-usage.json")               # 마지막으로 읽은 한도 사용률 — 재시작해도 유지


def codex_used_percent():
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            return float(json.load(f)["used_percent"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def pick_model():
    return CODEX_LIGHT_MODEL if codex_used_percent() >= CODEX_LIGHT_AT else CODEX_MODEL


def record_usage(thread_id):
    """이번 실행(thread_id)의 세션 기록에서 주간 한도 사용률을 읽어 저장하고, 그 기록 파일만 지운다.
    세션 기록에는 팀 코드(프롬프트)가 그대로 남으므로 읽자마자 지운다. 다른 Codex 대화 기록은 건드리지 않는다."""
    if not thread_id:
        return None
    used = None
    for dirpath, _, names in os.walk(os.path.join(CODEX_HOME, "sessions")):
        for name in names:
            path = os.path.join(dirpath, name)
            if not name.endswith(f"{thread_id}.jsonl"):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        if '"rate_limits"' not in line:
                            continue
                        limits = json.loads(line).get("payload", {}).get("rate_limits") or {}
                        # 주간 창(window_minutes=10080)만 본다 — 5시간 창은 금방 풀리므로 모델 전환 기준에서 뺀다
                        weekly = [w.get("used_percent") for w in (limits.get("primary"), limits.get("secondary"))
                                  if w and w.get("window_minutes") == WEEK_MINUTES]
                        weekly = [v for v in weekly if isinstance(v, (int, float))]
                        if weekly:
                            used = weekly[0]
            except (OSError, ValueError):
                pass
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
    if used is not None:
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump({"used_percent": used, "at": time.time()}, f)
    return used
# Codex 에게 넘기지 않는 환경변수 — 토큰 유출 방지 + API 키 과금 경로 차단
CODEX_ENV_DROP = ("GITLAB_TOKEN", "GEMINI_API_KEY", "WEBHOOK_SECRET", "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")


def codex_auth():
    """ChatGPT 구독 로그인일 때만 auth.json 을 돌려준다. API 키 로그인이면 None — 쓴 만큼 과금되는 경로는 타지 않는다."""
    try:
        with open(os.path.join(CODEX_HOME, "auth.json"), encoding="utf-8") as f:
            auth = json.load(f)
    except (OSError, ValueError):
        return None
    return auth if auth.get("auth_mode") == "chatgpt" and auth.get("tokens") else None


def codex_ask(system, text, root):
    if not codex_auth():
        raise RuntimeError("Codex ChatGPT 로그인 없음(API 키 로그인은 과금이라 쓰지 않음)")
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex CLI 없음")
    out = os.path.join(tempfile.mkdtemp(prefix="codex-out-"), "last.txt")
    # --ephemeral 을 쓰지 않는다 — 한도 사용률이 세션 기록에만 남기 때문(읽은 뒤 record_usage 가 지운다)
    cmd = [exe, "exec", "-", "-C", root, "--sandbox", "read-only", "--skip-git-repo-check", "--json",
           # 내 PC 설정(훅·모델)은 무시하고 필요한 것만 넘긴다 — 설정을 무시하면 추론 강도가 none 이 되므로 명시
           "--ignore-user-config", "-c", 'forced_login_method="chatgpt"',
           "-c", f'model_reasoning_effort="{os.environ.get("CODEX_EFFORT", "medium")}"', "-o", out]
    if os.name == "nt":  # Windows 로컬 실행: 이 샌드박스 모드가 없으면 파일 읽기 명령까지 전부 차단된다
        cmd += ["-c", 'windows.sandbox="elevated"']
    model = pick_model()
    cmd += ["-m", model]
    prompt = (system + "\n\n## 도구\n현재 작업 폴더가 이 커밋 시점의 저장소 전체다. 호출부·계약 상대 파일·테스트를 직접 읽어 확인하라. "
              "읽기 명령이 실패하면 주어진 내용만으로 판단하고 그 사실을 남겨라. "
              "파일을 수정하거나 네트워크를 쓰지 말고, 작업 폴더 밖은 읽지 마라.\n\n" + text)
    env = {k: v for k, v in os.environ.items() if k not in CODEX_ENV_DROP}
    env["CODEX_HOME"] = CODEX_HOME
    try:
        # ponytail: 한도 초과 문구를 따로 가려내지 않는다 — 실패면 전부 Gemini 로 넘긴다
        r = subprocess.run(cmd, input=prompt, env=env, capture_output=True, text=True, encoding="utf-8",
                           timeout=int(os.environ.get("CODEX_TIMEOUT", "900")))
        answer = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
    finally:
        shutil.rmtree(os.path.dirname(out), ignore_errors=True)
        thread = re.search(r'"thread_id":"([\w-]+)"', (r.stdout or "") if "r" in locals() else "")
        used = record_usage(thread and thread.group(1))
    if r.returncode != 0 or not answer.strip():
        raise RuntimeError(f"codex 실패(exit {r.returncode}): {r.stderr[-500:]}")
    print(f"[usage] engine=codex model={model} 한도사용={used if used is not None else '?'}% "
          f"(→ {CODEX_LIGHT_AT:g}% 이상이면 {CODEX_LIGHT_MODEL})", flush=True)
    return answer


def ask(prompt_file, text, root):
    """(엔진 이름, 답 원문). Codex 먼저, 실패하면 Gemini. 둘 다 일시 실패면 예외 → 폴러가 다음 주기에 재시도."""
    system = open(os.path.join(HERE, prompt_file), encoding="utf-8").read()
    try:
        return "Codex", codex_ask(system, text, root)
    except Exception as e:  # 한도 초과·로그인 만료·타임아웃 등
        print(f"[review] Codex 실패 → Gemini 로 대체: {e}", file=sys.stderr, flush=True)
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("Codex 실패, GEMINI_API_KEY 없음")
    return "Gemini", gemini_ask(system, text)


# ── 3) 테스트 결과 (Jenkins → GitLab 커밋 상태) ──────────────────
CI_WAIT_SEC = int(os.environ.get("CI_WAIT_SEC", "600"))  # 커밋 후 이 시간 동안 Jenkins 상태가 안 붙으면 테스트 결과 없이 진행
CI_PENDING = ("pending", "running", "created", "waiting_for_resource", "preparing")


def ci_results(mr, commits):
    """그 커밋의 Jenkins 결과 [(이름, 상태)]. 아직 도는 중이면 NotReady.
    봇은 MR 코드를 직접 실행하지 않는다 — 토큰·로그인 파일이 있는 곳에서 신뢰할 수 없는 코드를 돌리지 않기 위해."""
    latest = {}
    for st in sorted(gl_all(f"/repository/commits/{mr['sha']}/statuses"), key=lambda st: st["id"]):
        latest[st["name"]] = st["status"]  # 같은 이름이 여러 번 붙으면 마지막 것
    if any(v in CI_PENDING for v in latest.values()):
        raise NotReady("Jenkins 테스트 진행 중")
    if not latest:
        made = next((c["created_at"] for c in commits if c["id"] == mr["sha"]), None)
        if made:
            age = (datetime.datetime.now(datetime.timezone.utc)
                   - datetime.datetime.fromisoformat(made.replace("Z", "+00:00"))).total_seconds()
            if age < CI_WAIT_SEC:
                raise NotReady("Jenkins 결과 대기")
    return sorted(latest.items())


def ci_text(ci):
    if not ci:
        return "Jenkins 결과 없음(이 커밋에는 CI 가 돌지 않았거나 대기 시간이 지남)."
    return "\n".join(f"- {name}: {status}" for name, status in ci) + (
        "\n(back=Gradle build·테스트(Testcontainers PostgreSQL), ai=pytest·회귀, web=lint·build, app=Android check. "
        "실패 로그 본문은 권한 문제로 받지 못한다 — 상태만 근거로 쓴다.)")


SEVERITIES = ("필수", "제안", "질문")  # AI 지시문·JSON 안에서 쓰는 이름
# 코멘트에 보이는 이름 — 처음 보는 사람도 뜻을 알게
LABEL = {"필수": "필수", "제안": "수정 권장", "질문": "확인 필요"}


def parse_review(text):
    """에이전트 출력에서 마지막 ```json 블록을 읽는다. 형식이 깨졌으면 원문 텍스트를 그대로 돌려준다(리뷰를 버리지 않음)."""
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S) or re.findall(r"(\{.*\})", text, re.S)
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            data["findings"] = [f for f in data["findings"] if isinstance(f, dict) and f.get("severity") in SEVERITIES]
            return data
    return text.strip() or "_리뷰 결과 비어 있음. 사람이 직접 확인 필요._"


VERDICTS = ("확인", "반박", "불확실")


def parse_json(text):
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S) or re.findall(r"(\{.*\})", text, re.S)
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def merge(found, verified):
    """A 의 지적에 B 의 판정을 붙이고, B 가 새로 찾은 결함을 더한다. 반박된 지적은 따로 모은다."""
    findings = list(found.get("findings") or [])
    for v in (verified or {}).get("verdicts") or []:
        i = v.get("id")
        if isinstance(i, int) and 1 <= i <= len(findings) and v.get("verdict") in VERDICTS:
            findings[i - 1] = {**findings[i - 1], "verdict": v["verdict"], "verdict_reason": (v.get("reason") or "").strip()}
            if v.get("severity") in SEVERITIES:  # 검증자가 심각도를 바로잡았으면 따른다
                findings[i - 1]["severity"] = v["severity"]
    for f in (verified or {}).get("missed") or []:
        if isinstance(f, dict) and f.get("severity") in SEVERITIES:
            findings.append({**f, "verdict": "추가"})
    return {
        "findings": [f for f in findings if f.get("verdict") != "반박"],
        "rejected": [f for f in findings if f.get("verdict") == "반박"],
        "checked": found.get("checked") or [],
        "strengths": (verified or {}).get("strengths") or [],
        "tests": ((verified or {}).get("tests") or "").strip(),
        "verified": verified is not None,
    }


def redact(text):
    for p in SECRET_PATTERNS:
        text = p.sub("[삭제됨: 비밀값 형태]", text)
    values = [os.environ.get(n) for n in ("GITLAB_TOKEN", "GEMINI_API_KEY", "WEBHOOK_SECRET")]
    values += list(((codex_auth() or {}).get("tokens") or {}).values())  # Codex 로그인 토큰 원문도 지운다
    for value in values:
        if isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, "[삭제됨: 비밀값]")
    return text


# ── 게시 ───────────────────────────────────────────────────────
def file_link(mr, f):
    """짧은 표시(파일명:줄) + 그 커밋의 해당 줄로 가는 링크."""
    path = (f.get("file") or "").strip().lstrip("/")
    if not path:
        return ""
    a, b = f.get("line_start"), f.get("line_end")
    lines = f":{a}" + (f"-{b}" if b and b != a else "") if isinstance(a, int) else ""
    anchor = f"#L{a}" + (f"-{b}" if b and b != a else "") if isinstance(a, int) else ""
    url = f"{WEB}/-/blob/{mr['sha']}/{urllib.parse.quote(path)}{anchor}"
    return f" — [`{os.path.basename(path)}{lines}`]({url})"


def para(text):
    """목록 항목 아래 들여쓴 한 문단 — 토글 없이 바로 읽히게."""
    return "   " + " ".join(str(text).split())


def render_finding(n, mr, f):
    tag = {"확인": " · 검증: 확인됨", "불확실": " · 검증: 불확실", "추가": " · 검증 단계에서 추가"}.get(f.get("verdict"), "")
    out = [f"{n}. 🚨 [지적] **{(f.get('title') or '').strip()}**" + (" _(추정)_" if f.get("guess") else "") + tag + file_link(mr, f), ""]
    body = " ".join(f"**{label}**: {f[key].strip()}"
                    for label, key in (("문제 상황", "scenario"), ("근거", "evidence"), ("고치는 방법", "fix"), ("검증", "verdict_reason"))
                    if isinstance(f.get(key), str) and f[key].strip())
    if body:
        out += [para(body), ""]
    return out


def item(text):
    """섹션 아래 내용은 항상 한 단계 들여쓴다 — 마크다운에서 들여쓰기가 확실히 보이는 건 목록이라 목록 항목으로 만든다.
    여러 줄이면 이어지는 줄도 같은 항목 안에 머물게 들여쓴다."""
    return "- " + str(text).strip().replace("\n", "\n  ")


def render(mr, convention, review, ci=(), changed=0, engines=(), skipped=(), pending=False):
    """pending=True 면 결함 탐지(A)만 끝난 중간 결과 — 검증 전이라 표식을 pending 으로 둬 같은 커밋을 다시 보게 한다."""
    structured = isinstance(review, dict)
    findings = review["findings"] if structured else []
    count = {s: sum(f["severity"] == s for f in findings) for s in SEVERITIES}
    ci_line = "대기 중" if pending else ", ".join(f"{n} {s}" for n, s in ci) or "결과 없음"

    lines = [f"## 🔍 리뷰봇 · `{mr['sha'][:8]}`"]
    if pending:
        lines += ["", f"{PROGRESS} 결함 탐지 결과 먼저 게시 — Jenkins 테스트가 끝나면 검증해서 이 코멘트를 갱신함", ""]
    if structured:
        lines.append(" · ".join(f"{LABEL[s]} {count[s]}" for s in SEVERITIES) + f" · 변경 파일 {changed}개 · Jenkins: {ci_line}")
    lines += ["", "### [컨벤션]", ""]
    lines += [item(f"🚨 [지적] **{LABEL[lv]}** · {msg}") for lv, _, msg in convention] or [item("위반 없음")]
    lines.append("")

    if not structured:  # 모델이 형식을 못 지킨 경우 — 원문을 그대로 싣는다
        lines += ["### [코드 리뷰]", "", item(review), ""]
    else:
        if not review.get("verified") and not pending:
            lines += ["### [검증]", "", item("_검증 단계 실행 실패 — 아래 지적은 검증 안 됨._"), ""]
        for sev in SEVERITIES:
            items = [f for f in findings if f["severity"] == sev]
            if items:
                lines += [f"### [{LABEL[sev]}]", ""]
                for n, f in enumerate(items, 1):
                    lines += render_finding(n, mr, f)
        if not findings:
            lines += ["### [코드 리뷰]", "", item("발견한 결함 없음."), ""]
        strengths = [s for s in review.get("strengths") or [] if str(s).strip()]
        if strengths:
            lines += ["### [잘한 점]", ""] + [item(s) for s in strengths] + [""]

    who = " → ".join(engines) or "AI"
    lines += ["---", f"<sub>결함 탐지 → 검증: {who}. 테스트는 봇이 직접 돌리지 않고 Jenkins 결과를 근거로 함. "
              "AI 리뷰는 참고용이니 머지 전 사람이 직접 확인할 것.</sub>",
              f"{MARK}{'pending' if pending else mr['sha']} -->"]
    return redact("\n".join(lines))


def post(mr, body, notes=None):
    """봇 코멘트를 하나로 유지한다. 게시 직전에 코멘트 목록을 다시 읽는다 — 리뷰 시작 뒤에 단 '[리뷰 진행 중]'
    코멘트를 놓치면 결과가 새 코멘트로 달리고 진행 중 표시가 남기 때문. notes 인자는 예전 호출 호환용(쓰지 않음)."""
    headers = {"PRIVATE-TOKEN": os.environ["GITLAB_TOKEN"]}
    url = f"{GITLAB}/projects/{PROJECT}/merge_requests/{mr['iid']}/notes"
    mine = sorted((n for n in gl_all(f"/merge_requests/{mr['iid']}/notes") if MARK in n["body"]), key=lambda n: n["id"])
    if mine:  # 가장 먼저 단 봇 코멘트를 갱신 — MR 이 봇 코멘트로 도배되지 않게
        requests.put(f"{url}/{mine[0]['id']}", headers=headers, data={"body": body}, timeout=30).raise_for_status()
        for extra in mine[1:]:  # 예전 버그로 남은 중복 봇 코멘트 정리(봇 이름표가 있는 것만)
            requests.delete(f"{url}/{extra['id']}", headers=headers, timeout=30)
    else:
        requests.post(url, headers=headers, data={"body": body}, timeout=30).raise_for_status()


# 커밋 SHA → 에이전트 A 결과. A 를 Jenkins 와 동시에 돌려 두고, Jenkins 가 끝나면 B 만 돈다.
# ponytail: 메모리에만 둔다 — 봇을 재시작하면 대기 중이던 MR 은 A 를 한 번 더 돈다
A_DONE = {}


PROGRESS = "> **[리뷰 진행 중]**"


def mark_in_progress(mr, notes):
    """리뷰를 시작하자마자 MR 에 진행 중임을 보인다. 끝나면 post() 가 결과로 통째로 바꾼다.
    이전 커밋 리뷰가 있으면 지우지 않고 맨 위에 한 줄만 붙인다 — 새 리뷰가 끝날 때까지 이전 리뷰를 볼 수 있게."""
    line = (f"{PROGRESS} 커밋 `{mr['sha'][:8]}` 리뷰 중 — 결함 탐지 → Jenkins 테스트 대기 → 검증 순서로 진행, "
            "보통 10분 안팎 걸림")
    mine = next((n for n in notes if MARK in n["body"]), None)
    if mine:
        if line in mine["body"]:
            return
        old = "\n".join(l for l in mine["body"].splitlines() if not l.startswith(PROGRESS)).lstrip("\n")
        body = line + " (아래는 이전 커밋 리뷰)\n\n" + old
    else:
        body = f"## 🔍 리뷰봇 · `{mr['sha'][:8]}`\n\n{line}\n{MARK}pending -->"
    try:
        post(mr, body, notes)
    except requests.RequestException as e:  # 진행 표시는 부가 기능 — 실패해도 리뷰는 계속
        print(f"[review] 진행 중 표시 실패: {e}", file=sys.stderr, flush=True)


def review(iid, do_post, force=False):
    """MR 하나를 리뷰한다. 게시했으면 True, 이미 리뷰한 커밋이면 False, dry-run 이면 본문 문자열.
    force=True 면 같은 커밋이어도 다시 리뷰해 기존 코멘트를 갱신한다(형식·프롬프트를 바꾼 뒤 다시 달 때)."""
    mr = gl_get(f"/merge_requests/{iid}").json()
    notes = gl_all(f"/merge_requests/{iid}/notes")
    if do_post and not force and any(f"{MARK}{mr['sha']} -->" in n["body"] for n in notes):
        return False  # 같은 커밋 재리뷰 방지 — 재시작·중복 감지에도 무료 한도를 두 번 쓰지 않는다
    if do_post and mr["sha"] not in A_DONE:
        mark_in_progress(mr, notes)  # 무거운 작업(diff·저장소 다운로드) 전에 먼저 — 커밋 직후 바로 보이게

    diffs = gl_all(f"/merge_requests/{iid}/diffs")
    commits = gl_all(f"/merge_requests/{iid}/commits")
    cached = A_DONE.get(mr["sha"])
    if cached:  # A 는 이미 끝났고 Jenkins 만 기다리는 중 — 아직이면 스냅샷도 받지 않고 NotReady
        ci = ci_results(mr, commits)
    convention = check_conventions(mr, commits, diffs)
    ws, root = extract_snapshot(mr)
    try:
        text, skipped = build_input(mr, diffs, root)
        if not cached:
            # 에이전트 A: 결함 사냥 — 테스트 결과가 필요 없으므로 Jenkins 를 기다리지 않고 바로 돈다
            cached = A_DONE[mr["sha"]] = ask("prompt.md", text, root)
            while len(A_DONE) > 20:  # 오래된 커밋 결과는 버린다(새 커밋이 오면 그 SHA 로 다시 돈다)
                A_DONE.pop(next(iter(A_DONE)))
            early = parse_review(cached[1])
            if do_post and isinstance(early, dict):  # Jenkins·검증을 기다리지 않고 지적부터 보여 준다
                try:
                    post(mr, render(mr, convention, merge(early, None), (), len(diffs), (cached[0],), skipped, pending=True))
                except requests.RequestException as e:  # 중간 게시는 부가 기능 — 실패해도 리뷰는 계속
                    print(f"[review] 중간 결과 게시 실패: {e}", file=sys.stderr, flush=True)
            # B 는 테스트 결과를 봐야 하므로 여기서 Jenkins 를 기다린다. 아직이면 NotReady — A 결과는 A_DONE 에 남는다
            ci = ci_results(mr, commits)
        eng_a, raw_a = cached
        found = parse_review(raw_a)
        if not isinstance(found, dict):  # 형식이 깨지면 원문 그대로 싣고 검증은 건너뛴다
            body = render(mr, convention, found, ci, len(diffs), (eng_a,), skipped)
        else:
            # 에이전트 B: A 의 지적을 코드·테스트 결과로 하나씩 검증 + 놓친 결함 + 잘한 점 + 테스트 평가
            numbered = [{"id": i, **f} for i, f in enumerate(found["findings"], 1)]
            text_b = (text + "\n\n## Jenkins 테스트 결과 (이 커밋)\n" + ci_text(ci)
                      + "\n\n## 결함 탐지 리뷰어의 지적 (검증 대상)\n```json\n"
                      + json.dumps(numbered, ensure_ascii=False, indent=1) + "\n```")
            try:
                eng_b, raw_b = ask("verify.md", text_b, root)
                verified = parse_json(raw_b)
            except Exception as e:  # 검증이 실패해도 탐지 결과는 버리지 않는다
                print(f"[review] 검증 실패: {e}", file=sys.stderr, flush=True)
                eng_b, verified = None, None
            body = render(mr, convention, merge(found, verified), ci, len(diffs),
                          tuple(e for e in (eng_a, eng_b) if e), skipped)
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    A_DONE.pop(mr["sha"], None)
    if not do_post:
        return body
    post(mr, body, notes)
    return True


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    iid, do_post = int(sys.argv[1]), "--post" in sys.argv
    result = review(iid, do_post, force="--force" in sys.argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if result is True:
        print(f"게시 완료: MR !{iid}")
    elif result is False:
        print(f"이미 리뷰한 커밋이라 건너뜀: MR !{iid}")
    else:
        print(result)
        print("\n[dry-run] 게시하지 않았습니다.")


if __name__ == "__main__":
    main()
