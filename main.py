import os
import base64
import json
import re
import psycopg
from psycopg.rows import dict_row
from psycopg import sql as pgsql
import httpx
from io import BytesIO
from urllib.parse import quote
from fastapi.responses import Response
import shutil
import zipfile
import secrets
import hashlib
from datetime import timedelta

from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import bcrypt

from dotenv import load_dotenv

from fastapi import (
    FastAPI,
    HTTPException,
    UploadFile,
    File,
    Header,
    Depends,
)

from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from openai import OpenAI

from pydantic import (
    BaseModel,
    Field,
)

from hwpx_generator import generate_hwpx

# =========================================================
# 기본 설정
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("AILOG_DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "app.db")
TEMPLATE_PATH = os.path.join(BASE_DIR, "diary_template.hwpx")
DOCUMENT_DIR = os.path.join(DATA_DIR, "documents")
DRAFT_DIR = os.path.join(DOCUMENT_DIR, "draft")
FINAL_DIR = os.path.join(DOCUMENT_DIR, "final")
os.makedirs(DRAFT_DIR, exist_ok=True)
os.makedirs(FINAL_DIR, exist_ok=True)

load_dotenv(os.path.join(BASE_DIR, ".env"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini").strip()

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


app = FastAPI(
    title="개별 유아 보육일지 및 투약관리 API",
    description="AI 개별 유아 보육일지 생성 및 투약관리 서버",
    version="3.0.0",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[s.strip() for s in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:8080,http://127.0.0.1:8000").split(",") if s.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# 공통 함수
# =========================================================

KST = ZoneInfo("Asia/Seoul")


def now_text():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def today_text():
    return datetime.now(KST).strftime("%Y-%m-%d")


class DBConnection:
    """Compatibility wrapper preserving parameterized SQLite-style queries."""
    def __init__(self, conn):
        self.conn = conn
    def execute(self, query, params=()):
        # Existing SQL uses positional '?' only as parameter markers.
        return self.conn.execute(query.replace('?', '%s'), params)

@contextmanager
def get_db():
    url = os.getenv('DATABASE_URL', '').strip()
    if not url:
        raise RuntimeError('DATABASE_URL must be configured')
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=15) as conn:
        yield DBConnection(conn)

SUPABASE_URL = os.getenv('SUPABASE_URL', '').rstrip('/')
SUPABASE_SERVICE_ROLE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
STORAGE_BUCKET = 'ailog-documents'

def storage_request(method, object_key, *, content=None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(503, 'Supabase Storage 환경변수가 설정되지 않았습니다.')
    endpoint = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{quote(object_key, safe='/')}"
    headers = {'Authorization': f'Bearer {SUPABASE_SERVICE_ROLE_KEY}',
               'apikey': SUPABASE_SERVICE_ROLE_KEY}
    if content is not None:
        headers['Content-Type'] = 'application/octet-stream'
        headers['x-upsert'] = 'true'
    try:
        with httpx.Client(timeout=40) as http:
            response = http.request(method, endpoint, headers=headers, content=content)
        response.raise_for_status()
        return response.content
    except httpx.HTTPStatusError as exc:
        print("========== SUPABASE STORAGE ERROR ==========", flush=True)
        print("STATUS CODE:", exc.response.status_code, flush=True)
        print("ERROR BODY:", exc.response.text[:1000], flush=True)
        print("============================================", flush=True)

        raise HTTPException(
            status_code=502,
            detail="문서 저장소 요청에 실패했습니다.",
        ) from exc

    except httpx.RequestError as exc:
        print("========== SUPABASE STORAGE NETWORK ERROR ==========", flush=True)
        print("ERROR TYPE:", type(exc).__name__, flush=True)
        print("ERROR MESSAGE:", str(exc), flush=True)
        print("CAUSE:", repr(exc.__cause__), flush=True)
        print("====================================================", flush=True)

        raise HTTPException(
            status_code=502,
            detail="문서 저장소 연결에 실패했습니다.",
        ) from exc

def document_response(object_key):
    if not object_key:
        raise HTTPException(404, '문서가 없습니다.')
    data = storage_request('GET', object_key)
    filename = object_key.rsplit('/', 1)[-1]
    from urllib.parse import quote as url_quote
    return Response(content=data, media_type='application/octet-stream',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{url_quote(filename)}"})


def hash_password(password: str):
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")


def verify_password(password: str, password_hash: str):
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )

    except Exception:
        return False


# =========================================================
# 데이터베이스 생성
# =========================================================

def init_db():
    with get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (\n                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,\n                name TEXT NOT NULL,\n                email TEXT NOT NULL UNIQUE,\n                password_hash TEXT NOT NULL,\n                role TEXT NOT NULL\n                    CHECK(role IN ('teacher', 'guardian')),\n                created_at TEXT NOT NULL\n            )")
        conn.execute('CREATE TABLE IF NOT EXISTS auth_sessions (\n                token_hash TEXT PRIMARY KEY,\n                user_id INTEGER NOT NULL,\n                expires_at TEXT NOT NULL,\n                created_at TEXT NOT NULL,\n                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE\n            )')
        conn.execute("CREATE TABLE IF NOT EXISTS children (\n                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,\n\n                name TEXT NOT NULL,\n                class_name TEXT NOT NULL,\n\n                age TEXT DEFAULT '',\n\n                guardian_id INTEGER,\n\n                note TEXT DEFAULT '',\n\n                created_at TEXT NOT NULL,\n\n                FOREIGN KEY (guardian_id)\n                    REFERENCES users(id)\n                    ON DELETE SET NULL\n            )")
        conn.execute("CREATE TABLE IF NOT EXISTS child_diaries (\n                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,\n\n                child_id INTEGER,\n\n                child_name TEXT NOT NULL,\n                class_name TEXT DEFAULT '',\n                age TEXT DEFAULT '',\n\n                teacher_name TEXT DEFAULT '',\n\n                diary_date TEXT NOT NULL,\n\n                main_activity TEXT DEFAULT '',\n\n                activity_process TEXT DEFAULT '',\n\n                child_response TEXT DEFAULT '',\n\n                teacher_support TEXT DEFAULT '',\n\n                observation_evaluation TEXT DEFAULT '',\n\n                future_support TEXT DEFAULT '',\n\n                special_notes TEXT DEFAULT '',\n\n                family_connection TEXT DEFAULT '',\n\n                original_ocr TEXT DEFAULT '',\n\n                created_at TEXT NOT NULL,\n                updated_at TEXT NOT NULL,\n\n                FOREIGN KEY (child_id)\n                    REFERENCES children(id)\n                    ON DELETE SET NULL\n            )")
        conn.execute("CREATE TABLE IF NOT EXISTS medications (\n                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,\n\n                child_id INTEGER,\n                child_name TEXT NOT NULL,\n\n                medicine_name TEXT NOT NULL,\n                dosage TEXT NOT NULL,\n\n                medication_date TEXT DEFAULT '',\n                time TEXT NOT NULL,\n\n                memo TEXT DEFAULT '',\n\n                status TEXT NOT NULL DEFAULT 'pending'\n                    CHECK(status IN ('pending', 'completed')),\n\n                teacher_notified INTEGER NOT NULL DEFAULT 0,\n\n                created_at TEXT NOT NULL,\n                completed_at TEXT,\n\n                FOREIGN KEY (child_id)\n                    REFERENCES children(id)\n                    ON DELETE SET NULL\n            )")
        conn.execute("CREATE TABLE IF NOT EXISTS device_tokens (\n                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,\n\n                user_id INTEGER NOT NULL,\n\n                token TEXT NOT NULL UNIQUE,\n\n                platform TEXT DEFAULT 'android',\n\n                updated_at TEXT NOT NULL,\n\n                FOREIGN KEY (user_id)\n                    REFERENCES users(id)\n                    ON DELETE CASCADE\n            )")
        conn.execute('CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS\n            idx_child_diaries_child_name\n            ON child_diaries(child_name)')
        conn.execute('CREATE INDEX IF NOT EXISTS\n            idx_child_diaries_date\n            ON child_diaries(diary_date)')
        conn.execute('CREATE INDEX IF NOT EXISTS\n            idx_medications_child_name\n            ON medications(child_name)')
        conn.execute('CREATE INDEX IF NOT EXISTS\n            idx_medications_status\n            ON medications(status)')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id)")
        conn.execute("ALTER TABLE child_diaries ADD COLUMN IF NOT EXISTS draft_document_path TEXT DEFAULT ''")
        conn.execute("ALTER TABLE child_diaries ADD COLUMN IF NOT EXISTS final_document_path TEXT DEFAULT ''")
        conn.execute("ALTER TABLE child_diaries ADD COLUMN IF NOT EXISTS review_status TEXT DEFAULT 'WAITING_FOR_REVIEW'")
        conn.execute("ALTER TABLE child_diaries ADD COLUMN IF NOT EXISTS life_theme TEXT DEFAULT ''")
        conn.execute("ALTER TABLE child_diaries ADD COLUMN IF NOT EXISTS theme TEXT DEFAULT ''")
        conn.execute("ALTER TABLE child_diaries ADD COLUMN IF NOT EXISTS subtheme TEXT DEFAULT ''")
        conn.execute("ALTER TABLE child_diaries ADD COLUMN IF NOT EXISTS teacher_expectation TEXT DEFAULT ''")
        conn.execute("ALTER TABLE child_diaries ADD COLUMN IF NOT EXISTS schedule_json TEXT DEFAULT '[]'")

        if os.getenv('ENABLE_DEMO_SEED', '').lower() != 'true':
            return
        if len(os.getenv('DEMO_PASSWORD', '')) < 12:
            raise RuntimeError('Demo seed requires DEMO_PASSWORD with at least 12 characters')
        teacher = conn.execute(
            """
            SELECT id
            FROM users
            WHERE email = ?
            """,
            ("teacher@test.com",),
        ).fetchone()

        if teacher is None:

            conn.execute(
                """
                INSERT INTO users (
                    name,
                    email,
                    password_hash,
                    role,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "김교사",
                    "teacher@test.com",
                    hash_password(os.getenv("DEMO_PASSWORD", "1234")),
                    "teacher",
                    now_text(),
                ),
            )

        # -------------------------------------------------
        # 기본 보호자 계정
        # -------------------------------------------------

        guardian = conn.execute(
            """
            SELECT id
            FROM users
            WHERE email = ?
            """,
            ("parent@test.com",),
        ).fetchone()

        if guardian is None:

            cursor = conn.execute(
                """
                INSERT INTO users (
                    name,
                    email,
                    password_hash,
                    role,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?) RETURNING id
                """,
                (
                    "김보호자",
                    "parent@test.com",
                    hash_password(os.getenv("DEMO_PASSWORD", "1234")),
                    "guardian",
                    now_text(),
                ),
            )

            guardian_id = cursor.fetchone()["id"]

        else:
            guardian_id = guardian["id"]

        # -------------------------------------------------
        # 기본 유아
        # -------------------------------------------------

        count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM children
            """
        ).fetchone()["count"]

        if count == 0:

            conn.execute(
                """
                INSERT INTO children (
                    name,
                    class_name,
                    age,
                    guardian_id,
                    note,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "민들레",
                    "새싹반",
                    "5",
                    guardian_id,
                    "",
                    now_text(),
                ),
            )

            conn.execute(
                """
                INSERT INTO children (
                    name,
                    class_name,
                    age,
                    guardian_id,
                    note,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "철쭉",
                    "꽃잎반",
                    "5",
                    None,
                    "",
                    now_text(),
                ),
            )

            conn.execute(
                """
                INSERT INTO children (
                    name,
                    class_name,
                    age,
                    guardian_id,
                    note,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "팬지",
                    "열매반",
                    "5",
                    None,
                    "",
                    now_text(),
                ),
            )


# =========================================================
# Pydantic 입력 모델
# =========================================================

class LoginInput(BaseModel):
    email: str
    password: str


class SignupInput(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    email: str
    password: str = Field(min_length=12, max_length=100)
    role: str
    child_name: str = ""


class GenerateDiaryInput(BaseModel):

    text: str = Field(min_length=1)
    child_name: str = ""
    class_name: str = ""
    age: str = ""
    teacher_name: str = ""
    diary_date: str = ""
    life_theme: str = ""
    theme: str = ""
    subtheme: str = ""
    teacher_expectation: str = ""


class ScheduleItem(BaseModel):

    time: str
    activity: str = ""
    activity_process: str = ""
    child_response: str = ""
    teacher_support: str = ""
    remarks: str = ""


class SaveDiaryInput(BaseModel):

    child_name: str
    class_name: str = ""
    age: str = ""
    teacher_name: str = ""
    diary_date: str = ""

    # 기존 Flutter 화면과의 호환성을 위해 유지
    main_activity: str = ""
    activity_process: str = ""
    child_response: str = ""
    teacher_support: str = ""

    # 새 HWPX 양식용 항목
    life_theme: str = ""
    theme: str = ""
    subtheme: str = ""
    teacher_expectation: str = ""
    schedule: list[ScheduleItem] = Field(default_factory=list)

    observation_evaluation: str = ""
    future_support: str = ""
    special_notes: str = ""
    family_connection: str = ""
    original_ocr: str = ""


class MedicationInput(BaseModel):

    child_name: str

    medicine_name: str

    dosage: str

    time: str

    medication_date: str = ""

    memo: str = ""


class DeviceTokenInput(BaseModel):

    user_id: int

    token: str

    platform: str = "android"


# =========================================================
# 서버 상태
# =========================================================

@app.get("/")
def root():

    return {
        "status": "ok",
        "service": "개별 유아 보육일지 및 투약관리",
        "database": "PostgreSQL",
        "ai_configured": bool(OPENAI_API_KEY),
        "time": now_text(),
    }


@app.get("/health")
def health():

    with get_db() as conn:
        conn.execute("SELECT 1").fetchone()

    return {
        "status": "healthy",
        "database": "connected",
        "ai_configured": bool(OPENAI_API_KEY),
    }


# =========================================================
# 인증 세션 (토큰 원문은 DB에 저장하지 않음)
# =========================================================

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def current_user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="인증 토큰이 없습니다.")
    with get_db() as conn:
        row = conn.execute("""
            SELECT u.id, u.name, u.email, u.role, s.expires_at
            FROM auth_sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
        """, (_token_hash(token),)).fetchone()
        if row is None or datetime.fromisoformat(row["expires_at"]) <= datetime.now():
            raise HTTPException(status_code=401, detail="로그인이 만료되었습니다.")
        return dict(row)



# 역할 및 유아 소유권은 서버에서 검증합니다.
def require_teacher(user=Depends(current_user)):
    if user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="교사만 이용할 수 있습니다.")
    return user


def require_guardian(user=Depends(current_user)):
    if user["role"] != "guardian":
        raise HTTPException(status_code=403, detail="보호자만 이용할 수 있습니다.")
    return user


def assert_own_child(conn, user, child_id):
    row = conn.execute("SELECT id FROM children WHERE id = ? AND guardian_id = ?", (child_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(status_code=403, detail="연결된 유아만 조회할 수 있습니다.")


def assert_diary_access(conn, user, diary, *, draft=False):
    if user["role"] == "teacher":
        return
    if draft or diary["review_status"] != "REVIEW_COMPLETED":
        raise HTTPException(status_code=403, detail="검수 완료된 일지만 조회할 수 있습니다.")
    if diary["child_id"] is None:
        raise HTTPException(status_code=403, detail="유아 연결 정보가 없습니다.")
    assert_own_child(conn, user, diary["child_id"])


def public_diary(row):
    data = dict(row)
    # 보호자에게는 검수 완료 일지만 노출하며, 원본 OCR은 제외
    data.pop("original_ocr", None)
    return data

@app.post("/ocr")
async def recognize_photo(file: UploadFile = File(...), user=Depends(require_teacher)):
    if client is None:
        raise HTTPException(status_code=503, detail="OpenAI API Key가 설정되지 않았습니다.")
    # Flutter Web의 MultipartFile.fromBytes는 Content-Type을
    # application/octet-stream으로 전송할 수 있으므로 실제 파일 바이트로 판별합니다.
    content = await file.read(10 * 1024 * 1024 + 1)
    if not content or len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="이미지는 10MB 이하여야 합니다.")
    if content.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif content.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise HTTPException(status_code=415, detail="JPG, PNG, WEBP, GIF 이미지만 지원합니다.")
    image_data = base64.b64encode(content).decode("ascii")
    try:
        result = client.responses.create(
            model=os.getenv("OCR_MODEL", "gpt-4.1-mini"),
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": "사진에서 읽을 수 있는 한국어 및 다른 문자를 그대로 전사하세요. 추측하거나 없는 글자를 추가하지 마세요. 전사한 글자만 출력하세요."},
                {"type": "input_image", "image_url": f"data:{mime};base64,{image_data}"}
            ]}],
        )
        return {"text": result.output_text.strip()}
    except Exception:
        raise HTTPException(status_code=502, detail="사진 OCR 요청이 실패했습니다.")


@app.get("/me")
def get_me(user=Depends(current_user)):
    with get_db() as conn:
        child = conn.execute("SELECT name FROM children WHERE guardian_id = ? ORDER BY id LIMIT 1", (user["id"],)).fetchone() if user["role"] == "guardian" else None
    return {"user": {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"], "child_name": child["name"] if child else ""}}


@app.post("/logout")
def logout(authorization: str | None = Header(default=None), user=Depends(current_user)):
    token = authorization[7:].strip()
    with get_db() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_token_hash(token),))
    return {"success": True}


# =========================================================
# 로그인
# =========================================================

@app.post("/login")
def login(data: LoginInput):

    email = data.email.strip().lower()

    with get_db() as conn:

        user = conn.execute(
            """
            SELECT *
            FROM users
            WHERE email = ?
            """,
            (email,),
        ).fetchone()

        if user is None:

            raise HTTPException(
                status_code=401,
                detail="이메일 또는 비밀번호가 올바르지 않습니다.",
            )

        if not verify_password(
            data.password,
            user["password_hash"],
        ):

            raise HTTPException(
                status_code=401,
                detail="이메일 또는 비밀번호가 올바르지 않습니다.",
            )

        child_name = ""

        if user["role"] == "guardian":

            child = conn.execute(
                """
                SELECT name
                FROM children
                WHERE guardian_id = ?
                ORDER BY id
                LIMIT 1
                """,
                (user["id"],),
            ).fetchone()

            if child:
                child_name = child["name"]

        token = secrets.token_urlsafe(32)
        expires_at = (datetime.now() + timedelta(days=7)).isoformat()
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (_token_hash(token), user["id"], expires_at, now_text()),
        )
        return {
            "success": True,
            "access_token": token,
            "token_type": "bearer",
            "expires_at": expires_at,

            "user": {
                "id": user["id"],
                "name": user["name"],
                "email": user["email"],
                "role": user["role"],
                "child_name": child_name,
            },
        }


# =========================================================
# 회원가입
# =========================================================

@app.post("/signup")
def signup(data: SignupInput):

    email = data.email.strip().lower()
    role = data.role.strip().lower()

    if role != "guardian":

        raise HTTPException(
            status_code=400,
            detail="교사 계정은 공개 회원가입으로 생성할 수 없습니다.",
        )

    with get_db() as conn:

        existing = conn.execute(
            """
            SELECT id
            FROM users
            WHERE email = ?
            """,
            (email,),
        ).fetchone()

        if existing:

            raise HTTPException(
                status_code=409,
                detail="이미 가입된 이메일입니다.",
            )

        cursor = conn.execute(
            """
            INSERT INTO users (
                name,
                email,
                password_hash,
                role,
                created_at
            )
            VALUES (?, ?, ?, ?, ?) RETURNING id
            """,
            (
                data.name.strip(),
                email,
                hash_password(data.password),
                role,
                now_text(),
            ),
        )

        user_id = cursor.fetchone()["id"]

        # 가입 시 이름만으로 유아 소유권을 변경하지 않습니다.
        # 유아 연결은 추후 교사/관리자 승인 기능으로 처리합니다.

        return {
            "success": True,
            "message": "회원가입 완료",
            "user_id": user_id,
        }


# =========================================================
# 유아
# =========================================================

@app.get("/children")
def get_children(user=Depends(require_teacher)):

    with get_db() as conn:

        rows = conn.execute(
            """
            SELECT
                c.id,
                c.name,
                c.class_name,
                c.age,
                c.note,
                c.guardian_id,
                u.name AS guardian_name

            FROM children c

            LEFT JOIN users u
                ON c.guardian_id = u.id

            ORDER BY c.id
            """
        ).fetchall()

        return [dict(row) for row in rows]


@app.get("/children/{child_id}")
def get_child(child_id: int, user=Depends(require_teacher)):

    with get_db() as conn:

        child = conn.execute(
            """
            SELECT
                c.id,
                c.name,
                c.class_name,
                c.age,
                c.note,
                c.guardian_id,
                u.name AS guardian_name

            FROM children c

            LEFT JOIN users u
                ON c.guardian_id = u.id

            WHERE c.id = ?
            """,
            (child_id,),
        ).fetchone()

        if child is None:

            raise HTTPException(
                status_code=404,
                detail="유아를 찾을 수 없습니다.",
            )

        return dict(child)


class LinkGuardianInput(BaseModel):
    guardian_id: int

@app.get("/guardians")
def list_guardians(user=Depends(require_teacher)):
    with get_db() as conn:
        rows=conn.execute("SELECT id, name, email FROM users WHERE role='guardian' ORDER BY id DESC").fetchall()
        return [dict(x) for x in rows]

@app.post("/children/{child_id}/link-guardian")
def link_guardian(child_id: int, data: LinkGuardianInput, user=Depends(require_teacher)):
    with get_db() as conn:
        child=conn.execute("SELECT id, guardian_id FROM children WHERE id=?", (child_id,)).fetchone()
        if child is None:
            raise HTTPException(status_code=404, detail="유아를 찾을 수 없습니다.")
        guardian=conn.execute("SELECT id FROM users WHERE id=? AND role='guardian'",(data.guardian_id,)).fetchone()
        if guardian is None:
            raise HTTPException(status_code=404, detail="보호자를 찾을 수 없습니다.")
        if child["guardian_id"] not in (None, data.guardian_id):
            raise HTTPException(status_code=409, detail="이미 다른 보호자와 연결되어 있습니다.")
        conn.execute("UPDATE children SET guardian_id=? WHERE id=?", (data.guardian_id,child_id))
        return {"success":True, "child_id":child_id, "guardian_id":data.guardian_id}

@app.get("/guardians/{guardian_id}/child")
def guardian_child(guardian_id: int, user=Depends(require_guardian)):
    if guardian_id != user["id"]:
        raise HTTPException(status_code=403, detail="본인 정보만 조회할 수 있습니다.")

    with get_db() as conn:

        child = conn.execute(
            """
            SELECT
                id,
                name,
                class_name,
                age,
                note,
                guardian_id

            FROM children

            WHERE guardian_id = ?

            LIMIT 1
            """,
            (guardian_id,),
        ).fetchone()

        if child is None:

            raise HTTPException(
                status_code=404,
                detail="연결된 유아가 없습니다.",
            )

        return dict(child)


# =========================================================
# AI 개별 유아 보육일지 생성 + 시간대별 검증
# =========================================================

# 정시별 9개 슬롯이 HWPX 양식의 9개 행과 1:1로 대응합니다.
# AI가 일부 슬롯만 반환하거나 순서가 바뀌어도 시간값 기준으로 처리합니다.
DIARY_SLOT_TIMES = [f"{hour:02d}:00~{hour + 1:02d}:00" for hour in range(9, 18)]
SCHEDULE_KEYS = ("activity", "activity_process", "child_response", "teacher_support", "remarks")
TIME_RANGE = re.compile(r"(?<![0-9])([01]?[0-9]|2[0-3])\s*[:：]\s*([0-5][0-9])\s*[~∼～\-–—]\s*([01]?[0-9]|2[0-3])\s*[:：]\s*([0-5][0-9])(?![0-9])")
TIME_POINT = re.compile(r"(?<![0-9])([01]?[0-9]|2[0-3])\s*[:：]\s*([0-5][0-9])(?![0-9])")


def _slots_for_time_label(label: str) -> list[str]:
    """Return the hour slots overlapping an explicitly written time/range."""
    match = TIME_RANGE.search(str(label))
    if match:
        start_hour, start_min, end_hour, end_min = (int(v) for v in match.groups())
        start = start_hour * 60 + start_min
        end = end_hour * 60 + end_min
        if end <= start:  # 날짜가 넘어가는 시간은 양식에 임의 배정하지 않음
            return []
        return [
            f"{hour:02d}:00~{hour + 1:02d}:00"
            for hour in range(9, 18)
            if start < (hour + 1) * 60 and end > hour * 60
        ]
    point = TIME_POINT.search(str(label))
    if point:
        hour = int(point.group(1))
        if 9 <= hour < 18:
            return [f"{hour:02d}:00~{hour + 1:02d}:00"]
    return []


def _ocr_explicit_slots(note: str) -> set[str]:
    """Only identify slots for explicit numeric times in the OCR text."""
    referenced = set()
    for match in TIME_RANGE.finditer(note):
        referenced.update(_slots_for_time_label(match.group(0)))
    # Prevent the end boundary of an interval from counting as a new activity.
    remaining = TIME_RANGE.sub(' ', note)
    for match in TIME_POINT.finditer(remaining):
        referenced.update(_slots_for_time_label(match.group(0)))
    return referenced


def _empty_schedule() -> list[dict]:
    return [{"time": slot, **{key: "" for key in SCHEDULE_KEYS}} for slot in DIARY_SLOT_TIMES]


def _normalize_ai_schedule(raw_schedule) -> list[dict]:
    """Align sparse, unordered, or minute-specific AI entries to template rows."""
    timetable = {row["time"]: row for row in _empty_schedule()}
    if not isinstance(raw_schedule, list):
        return list(timetable.values())
    for item in raw_schedule:
        if not isinstance(item, dict):
            continue
        # Ex: 09:30~10:20 correctly overlaps both 09~10 and 10~11.
        slots = _slots_for_time_label(str(item.get('time', '')))
        for slot in slots:
            dest = timetable[slot]
            for key in SCHEDULE_KEYS:
                value = str(item.get(key) or '').strip()
                if value and value not in dest[key].split('\n'):
                    dest[key] = (dest[key] + '\n' + value).strip() if dest[key] else value
    return list(timetable.values())


def _parse_ai_json(text: str) -> dict:
    raw = text.strip()
    if raw.startswith('```'):
        raw = raw.replace('```json', '').replace('```JSON', '').replace('```', '').strip()
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError('AI 응답이 JSON 객체가 아닙니다.')
    return obj


@app.post("/generate")
def generate_diary(data: GenerateDiaryInput, user=Depends(require_teacher)):
    ocr_text = data.text.strip()
    if not ocr_text:
        raise HTTPException(status_code=400, detail="교사 메모 내용이 없습니다.")
    if client is None:
        raise HTTPException(status_code=503, detail="OpenAI API Key가 설정되지 않았습니다.")

    slot_blueprint = json.dumps(_empty_schedule(), ensure_ascii=False)
    prompt = f"""
너는 어린이집 교사의 개별 유아 보육일지 작성을 돕는 AI이다.
아래 [교사 메모 OCR]에 실제로 기록된 사실만 사용하라.
새로운 놀이, 시간, 유아의 발화, 교사의 행동을 추측해서 만들지 마라.

[유아 정보]
유아명: {data.child_name}
반명: {data.class_name}
연령: {data.age}
담당교사: {data.teacher_name}
생활주제 참고: {data.life_theme}
주제 참고: {data.theme}
소주제 참고: {data.subtheme}
교사의 기대 참고: {data.teacher_expectation}

[교사 메모 OCR]
{ocr_text}

[시간표 작성 규칙 - 중요]
1. 09:00~10:00부터 17:00~18:00까지 아래 JSON의 9개 시간대 모두를 반드시 반환한다.
2. OCR 메모에 여러 시간대가 있다면 일부만 작성하지 말고 모두 검토해 해당 구간에 채워라.
3. 09:30~10:20의 활동은 실제 시간이 겹치는 09~10시와 10~11시 구간 모두에 기술할 수 있다.
4. 10:30~10:50, 10:50~11:10 등 세부 시간은 해당 정시 구간에 정확히 배치한다.
5. 시간 구간에 대응하는 기록이 없으면 그 구간의 다른 값은 모두 빈 문자열로 둔다.
6. 일과 및 활동(activity), 활동내용 및 실행(activity_process), 유아의 놀이모습(child_response),
   교사의 지원 및 평가(teacher_support), 비고(remarks)를 각각 근거에 따라 채워라.
7. 서로 다른 시간대를 하나의 시간대에 모두 몰아서 작성하지 마라.
8. 생활주제, 주제, 소주제, 교사의 기대 및 하단 서술 항목도 기록에 근거해서 작성하라.
9. 특이사항이 없으면 빈 문자열로 둔다. 의학적 진단이나 확인되지 않은 사실을 추가하지 마라.
10. 모든 값을 한국어 문장으로 쓰되 JSON 키 이름과 time 값은 정확히 유지하라.

[반환 구조]
반드시 JSON 객체 하나만 반환하고 마크다운이나 설명을 붙이지 마라.
"life_theme", "theme", "subtheme", "teacher_expectation"과
"main_activity", "activity_process", "child_response", "teacher_support",
"observation_evaluation", "future_support", "special_notes", "family_connection"은 문자열이다.
"schedule"은 반드시 다음의 9개 원소를 모두 포함해야 한다.
{slot_blueprint}
"""

    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        result = _parse_ai_json(response.output_text)

        fields = (
            "life_theme", "theme", "subtheme", "teacher_expectation",
            "main_activity", "activity_process", "child_response", "teacher_support",
            "observation_evaluation", "future_support", "special_notes", "family_connection",
        )
        clean_result = {
            field: str(result.get(field) or '').strip() for field in fields
        }
        clean_result['schedule'] = _normalize_ai_schedule(result.get('schedule'))

        # If the OCR clearly mentions other hours but the AI missed them,
        # make one targeted repair request instead of silently losing the day.
        expected = _ocr_explicit_slots(ocr_text)
        populated = {
            row['time'] for row in clean_result['schedule']
            if any(row.get(field) for field in SCHEDULE_KEYS)
        }
        missing = sorted(expected - populated)
        if missing:
            try:
                repair_prompt = (
                    "다음 교사 OCR 메모에 명시된 시간대 중 첫 응답에서 누락된 시간대만 보완하라. "
                    "확인되지 않은 사실은 절대 추가하지 말고 빈 문자열로 둔다. "
                    "schedule 배열만 포함한 JSON 객체 하나를 반환하라. "
                    "time은 해당 정시 구간 그대로, 각 원소는 time, activity, activity_process, "
                    "child_response, teacher_support, remarks를 포함한다.\n"
                    f"누락된 정시 구간: {', '.join(missing)}\n"
                    f"OCR 메모:\n{ocr_text}"
                )
                repaired = _parse_ai_json(
                    client.responses.create(model=OPENAI_MODEL, input=repair_prompt).output_text
                )
                additions = _normalize_ai_schedule(repaired.get('schedule'))
                by_time = {row['time']: row for row in clean_result['schedule']}
                for row in additions:
                    if row['time'] not in missing:
                        continue
                    for key in SCHEDULE_KEYS:
                        if row[key] and not by_time[row['time']][key]:
                            by_time[row['time']][key] = row[key]
            except Exception as exc:
                # 보완 API가 실패해도 정상 생성된 내용까지 잃지 않게 유지한다.
                print('AI timetable repair skipped:', type(exc).__name__, flush=True)
        return clean_result

    except (json.JSONDecodeError, ValueError) as exc:
        print('AI JSON format error:', type(exc).__name__, flush=True)
        raise HTTPException(status_code=502, detail="AI 응답을 보육일지 형식으로 변환하지 못했습니다.") from exc
    except Exception as exc:
        print('AI generation error:', type(exc).__name__, str(exc)[:250], flush=True)
        raise HTTPException(status_code=502, detail="AI 보육일지 생성 중 오류가 발생했습니다.") from exc


# =========================================================
# 보육일지 저장
# =========================================================

@app.post("/save")
def save_diary(data: SaveDiaryInput, user=Depends(require_teacher)):

    child_name = data.child_name.strip()

    if not child_name:

        raise HTTPException(
            status_code=400,
            detail="유아를 선택해주세요.",
        )

    with get_db() as conn:

        child = conn.execute(
            """
            SELECT *
            FROM children
            WHERE name = ?
            """,
            (child_name,),
        ).fetchone()

        if child is None:

            raise HTTPException(
                status_code=404,
                detail="등록되지 않은 유아입니다.",
            )

        class_name = (
            data.class_name.strip()
            if data.class_name.strip()
            else child["class_name"]
        )

        age = (
            data.age.strip()
            if data.age.strip()
            else child["age"]
        )

        diary_date = (
            data.diary_date.strip()
            if data.diary_date.strip()
            else today_text()
        )

        current_time = now_text()

        cursor = conn.execute(
            """
            INSERT INTO child_diaries (

                child_id,
                child_name,
                class_name,
                age,
                teacher_name,
                diary_date,

                main_activity,
                activity_process,
                child_response,
                teacher_support,
                observation_evaluation,
                future_support,
                special_notes,
                family_connection,

                original_ocr,

                created_at,
                updated_at
            )

            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?,
                ?, ?
            ) RETURNING id
            """,
            (
                child["id"],
                child_name,
                class_name,
                age,
                user["name"],
                diary_date,

                data.main_activity.strip(),
                data.activity_process.strip(),
                data.child_response.strip(),
                data.teacher_support.strip(),
                data.observation_evaluation.strip(),
                data.future_support.strip(),
                data.special_notes.strip(),
                data.family_connection.strip(),

                data.original_ocr.strip(),

                current_time,
                current_time,
            ),
        )

        diary_id = cursor.fetchone()["id"]

        # 저장된 내용을 확정된 HWPX 양식에 자동 입력해 초안 생성
        safe_child_name = "".join(
            c for c in child_name if c.isalnum() or c in ("_", "-")
        ) or f"child_{child['id']}"
        draft_filename = f"{safe_child_name}_{diary_date}_보육일지_초안_{diary_id}.hwpx"
        draft_path = os.path.join(DRAFT_DIR, draft_filename)
        draft_key = f"draft/{diary_id}/diary_draft_{diary_id}.hwpx"

        document_data = {
            "child_name": child_name,
            "class_name": class_name,
            "age": age,
            "teacher_name": user["name"],
            "diary_date": diary_date,
            "life_theme": data.life_theme.strip(),
            "theme": data.theme.strip(),
            "subtheme": data.subtheme.strip(),
            "teacher_expectation": data.teacher_expectation.strip(),
            "schedule": [item.model_dump() for item in data.schedule],
            "main_activity": data.main_activity.strip(),
            "activity_process": data.activity_process.strip(),
            "child_response": data.child_response.strip(),
            "teacher_support": data.teacher_support.strip(),
            "observation_evaluation": data.observation_evaluation.strip(),
            "future_support": data.future_support.strip(),
            "special_notes": data.special_notes.strip(),
            "family_connection": data.family_connection.strip(),
        }

        try:
            generate_hwpx(TEMPLATE_PATH, draft_path, document_data)
            with open(draft_path, "rb") as f:
                storage_request("POST", draft_key, content=f.read())
            os.remove(draft_path)
        except Exception as e:
            print("HWPX GENERATION ERROR:", repr(e))
            raise HTTPException(
                status_code=500,
                detail=f"HWPX 초안 생성에 실패했습니다: {e}",
            )

        conn.execute(
            """
            UPDATE child_diaries
            SET draft_document_path = ?,
                review_status = 'WAITING_FOR_REVIEW',
                life_theme = ?,
                theme = ?,
                subtheme = ?,
                teacher_expectation = ?,
                schedule_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                draft_key,
                data.life_theme.strip(),
                data.theme.strip(),
                data.subtheme.strip(),
                data.teacher_expectation.strip(),
                json.dumps([item.model_dump() for item in data.schedule], ensure_ascii=False),
                now_text(),
                diary_id,
            ),
        )

        saved = conn.execute(
            """
            SELECT *
            FROM child_diaries
            WHERE id = ?
            """,
            (diary_id,),
        ).fetchone()

        return {
            "success": True,
            "message": "개별 유아 보육일지가 저장되었습니다.",
            "diary": public_diary(saved),
        }


# =========================================================
# 전체 일지
# =========================================================

@app.get("/diaries")
def all_diaries(user=Depends(require_teacher)):

    with get_db() as conn:

        rows = conn.execute(
            """
            SELECT *
            FROM child_diaries
            ORDER BY diary_date DESC, id DESC
            """
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]


# =========================================================
# 특정 유아 일지
# =========================================================

@app.get("/diaries/{child_name}")
def child_diaries(child_name: str, user=Depends(require_teacher)):

    with get_db() as conn:

        rows = conn.execute(
            """
            SELECT *
            FROM child_diaries

            WHERE child_name = ?

            ORDER BY diary_date DESC, id DESC
            """,
            (child_name,),
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]


# =========================================================
# 일지 하나 조회
# =========================================================

@app.get("/diary/{diary_id}")
def diary_detail(diary_id: int, user=Depends(current_user)):

    with get_db() as conn:

        diary = conn.execute(
            """
            SELECT *
            FROM child_diaries
            WHERE id = ?
            """,
            (diary_id,),
        ).fetchone()

        if diary is None:

            raise HTTPException(
                status_code=404,
                detail="보육일지를 찾을 수 없습니다.",
            )

        assert_diary_access(conn, user, diary)
        return dict(diary) if user["role"] == "teacher" else public_diary(diary)


# =========================================================
# 일지 삭제
# =========================================================

@app.delete("/diaries/{diary_id}")
def delete_diary(diary_id: int, user=Depends(require_teacher)):

    with get_db() as conn:

        diary = conn.execute(
            """
            SELECT id, draft_document_path, final_document_path
            FROM child_diaries
            WHERE id = ?
            """,
            (diary_id,),
        ).fetchone()

        if diary is None:

            raise HTTPException(
                status_code=404,
                detail="보육일지를 찾을 수 없습니다.",
            )

        conn.execute(
            """
            DELETE FROM child_diaries
            WHERE id = ?
            """,
            (diary_id,),
        )

    # 데이터베이스 삭제 후 Supabase의 연결 문서도 정리합니다.
    # Storage 장애가 발생해도 삭제된 일지가 다시 나타나지 않도록 처리합니다.
    for object_key in (diary.get("draft_document_path"), diary.get("final_document_path")):
        if object_key and object_key.startswith((f"draft/{diary_id}/", f"final/{diary_id}/")):
            try:
                storage_request("DELETE", object_key)
            except Exception as exc:
                print(f"Diary {diary_id} storage cleanup failed: {exc}", flush=True)

    return {
        "success": True,
        "message": "보육일지가 삭제되었습니다.",
    }


# =========================================================
# HWPX 초안 다운로드 / 검수본 업로드 / 최종본 다운로드
# =========================================================

@app.get("/diaries/{diary_id}/draft-document")
def download_draft_document(diary_id: int, user=Depends(require_teacher)):
    with get_db() as conn:
        diary = conn.execute(
            "SELECT * FROM child_diaries WHERE id = ?",
            (diary_id,),
        ).fetchone()

        if diary is None:
            raise HTTPException(status_code=404, detail="보육일지를 찾을 수 없습니다.")

        return document_response(diary['draft_document_path'] or '')


@app.post("/diaries/{diary_id}/reviewed-document")
async def upload_reviewed_document(
    diary_id: int,
    file: UploadFile = File(...),
    user=Depends(require_teacher),
):
    filename = file.filename or ""
    if not filename.lower().endswith(".hwpx"):
        raise HTTPException(status_code=400, detail="HWPX 파일만 업로드할 수 있습니다.")

    with get_db() as conn:
        diary = conn.execute(
            "SELECT * FROM child_diaries WHERE id = ?",
            (diary_id,),
        ).fetchone()

        if diary is None:
            raise HTTPException(status_code=404, detail="보육일지를 찾을 수 없습니다.")

        safe_child_name = "".join(
            c for c in diary["child_name"] if c.isalnum() or c in ("_", "-")
        ) or f"child_{diary_id}"
        final_filename = f"{safe_child_name}_{diary['diary_date']}_보육일지_최종_{diary_id}.hwpx"
        final_path = os.path.join(FINAL_DIR, final_filename)

        content = await file.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="HWPX 파일은 8MB 이하여야 합니다.")
        if not content:
            raise HTTPException(status_code=400, detail="업로드한 파일이 비어 있습니다.")

        try:
            with zipfile.ZipFile(BytesIO(content), 'r') as z:
                if z.testzip() is not None or 'Contents/section0.xml' not in z.namelist():
                    raise ValueError('올바른 HWPX 구조가 아닙니다.')
        except (zipfile.BadZipFile, ValueError) as exc:
            raise HTTPException(400, '올바른 HWPX 파일이 아닙니다.') from exc
        final_key = f'final/{diary_id}/diary_final_{diary_id}.hwpx'
        storage_request('POST', final_key, content=content)

        conn.execute(
            """
            UPDATE child_diaries
            SET final_document_path = ?,
                review_status = 'REVIEW_COMPLETED',
                updated_at = ?
            WHERE id = ?
            """,
            (final_key, now_text(), diary_id),
        )

        return {
            "success": True,
            "message": "교사 검수 완료본이 업로드되었습니다.",
            "diary_id": diary_id,
            "review_status": "REVIEW_COMPLETED",
            "final_filename": final_filename,
        }


@app.get("/diaries/{diary_id}/final-document")
def download_final_document(diary_id: int, user=Depends(current_user)):
    with get_db() as conn:
        diary = conn.execute(
            "SELECT * FROM child_diaries WHERE id = ?",
            (diary_id,),
        ).fetchone()

        if diary is None:
            raise HTTPException(status_code=404, detail="보육일지를 찾을 수 없습니다.")

        assert_diary_access(conn, user, diary)

        if diary["review_status"] != "REVIEW_COMPLETED":
            raise HTTPException(status_code=403, detail="아직 교사 검수가 완료되지 않았습니다.")

        return document_response(diary['final_document_path'] or '')


# 보호자용: 검수 완료된 일지만 조회
@app.get("/guardians/{guardian_id}/final-diaries")
def guardian_final_diaries(guardian_id: int, user=Depends(require_guardian)):
    if guardian_id != user["id"]:
        raise HTTPException(status_code=403, detail="본인 일지만 조회할 수 있습니다.")
    with get_db() as conn:
        child = conn.execute(
            "SELECT id, name FROM children WHERE guardian_id = ? LIMIT 1",
            (guardian_id,),
        ).fetchone()

        if child is None:
            raise HTTPException(status_code=404, detail="연결된 유아가 없습니다.")

        rows = conn.execute(
            """
            SELECT *
            FROM child_diaries
            WHERE child_id = ?
              AND review_status = 'REVIEW_COMPLETED'
            ORDER BY diary_date DESC, id DESC
            """,
            (child["id"],),
        ).fetchall()

        return [public_diary(row) for row in rows]


# =========================================================
# 투약 등록
# =========================================================

@app.post("/medications")
def create_medication(data: MedicationInput, user=Depends(require_guardian)):

    child_name = data.child_name.strip()

    if not child_name:

        raise HTTPException(
            status_code=400,
            detail="유아 이름이 필요합니다.",
        )

    if not data.medicine_name.strip():

        raise HTTPException(
            status_code=400,
            detail="약 이름을 입력해주세요.",
        )

    if not data.dosage.strip():

        raise HTTPException(
            status_code=400,
            detail="투약량을 입력해주세요.",
        )

    if not data.time.strip():

        raise HTTPException(
            status_code=400,
            detail="투약 시간을 입력해주세요.",
        )

    with get_db() as conn:

        child = conn.execute(
            """
            SELECT id
            FROM children
            WHERE name = ?
            """,
            (child_name,),
        ).fetchone()

        if child is None:

            raise HTTPException(
                status_code=404,
                detail="등록되지 않은 유아입니다.",
            )

        assert_own_child(conn, user, child["id"])

        medication_date = (
            data.medication_date.strip()
            if data.medication_date.strip()
            else today_text()
        )

        cursor = conn.execute(
            """
            INSERT INTO medications (

                child_id,
                child_name,

                medicine_name,
                dosage,

                medication_date,
                time,

                memo,

                status,
                teacher_notified,

                created_at,
                completed_at
            )

            VALUES (
                ?, ?,
                ?, ?,
                ?, ?,
                ?,
                'pending',
                0,
                ?,
                NULL
            ) RETURNING id
            """,
            (
                child["id"],
                child_name,

                data.medicine_name.strip(),
                data.dosage.strip(),

                medication_date,
                data.time.strip(),

                data.memo.strip(),

                now_text(),
            ),
        )

        medication_id = cursor.fetchone()["id"]

        medication = conn.execute(
            """
            SELECT *
            FROM medications
            WHERE id = ?
            """,
            (medication_id,),
        ).fetchone()

        return dict(medication)


# =========================================================
# 전체 투약 목록
# =========================================================

@app.get("/medications")
def all_medications(user=Depends(require_teacher)):

    with get_db() as conn:

        rows = conn.execute(
            """
            SELECT *
            FROM medications

            ORDER BY

                CASE
                    WHEN status = 'pending'
                    THEN 0
                    ELSE 1
                END,

                medication_date DESC,
                time ASC,
                id DESC
            """
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]


# =========================================================
# 유아별 투약 목록
# =========================================================

@app.get("/medications/{child_name}")
def child_medications(child_name: str, user=Depends(current_user)):

    with get_db() as conn:
        if user["role"] == "guardian":
            own = conn.execute("SELECT id FROM children WHERE name = ? AND guardian_id = ?", (child_name, user["id"])).fetchone()
            if own is None:
                raise HTTPException(status_code=403, detail="연결된 유아의 투약정보만 조회할 수 있습니다.")

        rows = conn.execute(
            """
            SELECT *
            FROM medications

            WHERE child_name = ?

            ORDER BY
                medication_date DESC,
                time DESC,
                id DESC
            """,
            (child_name,),
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]


# =========================================================
# 투약 완료
# =========================================================

@app.post("/medications/{medication_id}/complete")
def complete_medication(medication_id: int, user=Depends(require_teacher)):

    with get_db() as conn:

        medication = conn.execute(
            """
            SELECT *
            FROM medications
            WHERE id = ?
            """,
            (medication_id,),
        ).fetchone()

        if medication is None:

            raise HTTPException(
                status_code=404,
                detail="투약 정보를 찾을 수 없습니다.",
            )

        if medication["status"] == "completed":

            return {
                "success": True,
                "message": "이미 투약 완료된 항목입니다.",
                "id": medication_id,
                "status": "completed",
                "completed_at": medication["completed_at"],
            }

        completed_at = now_text()

        conn.execute(
            """
            UPDATE medications

            SET
                status = 'completed',
                completed_at = ?

            WHERE id = ?
            """,
            (
                completed_at,
                medication_id,
            ),
        )

        # Firebase 연결 단계에서
        # 여기서 해당 유아 보호자에게
        # 실제 투약 완료 FCM 푸시를 전송한다.

        return {
            "success": True,
            "message": "투약 완료 처리되었습니다.",
            "id": medication_id,
            "status": "completed",
            "completed_at": completed_at,
        }


# =========================================================
# 투약 삭제
# =========================================================

@app.delete("/medications/{medication_id}")
def delete_medication(medication_id: int, user=Depends(current_user)):

    with get_db() as conn:

        medication = conn.execute(
            """
            SELECT id, child_id, child_name
            FROM medications
            WHERE id = ?
            """,
            (medication_id,),
        ).fetchone()

        if medication is None:

            raise HTTPException(
                status_code=404,
                detail="투약 정보를 찾을 수 없습니다.",
            )

        if user["role"] == "guardian":
            # 보호자는 본인과 연결된 유아의 기록만 삭제할 수 있습니다.
            owned = conn.execute(
                "SELECT id FROM children WHERE guardian_id = ? AND (id = ? OR name = ?)",
                (user["id"], medication["child_id"], medication["child_name"]),
            ).fetchone()
            if owned is None:
                raise HTTPException(status_code=403, detail="연결된 유아의 투약기록만 삭제할 수 있습니다.")
        elif user["role"] != "teacher":
            raise HTTPException(status_code=403, detail="삭제 권한이 없습니다.")

        conn.execute(
            """
            DELETE FROM medications
            WHERE id = ?
            """,
            (medication_id,),
        )

        return {
            "success": True,
            "message": "투약 정보가 삭제되었습니다.",
        }


# =========================================================
# Firebase 기기 토큰 저장
# =========================================================

@app.post("/device-token")
def save_device_token(data: DeviceTokenInput, user=Depends(current_user)):
    if data.user_id != user["id"]:
        raise HTTPException(status_code=403, detail="본인 기기만 등록할 수 있습니다.")

    token = data.token.strip()

    if not token:

        raise HTTPException(
            status_code=400,
            detail="기기 토큰이 없습니다.",
        )

    with get_db() as conn:

        user = conn.execute(
            """
            SELECT id
            FROM users
            WHERE id = ?
            """,
            (data.user_id,),
        ).fetchone()

        if user is None:

            raise HTTPException(
                status_code=404,
                detail="사용자를 찾을 수 없습니다.",
            )

        existing = conn.execute(
            """
            SELECT id
            FROM device_tokens
            WHERE token = ?
            """,
            (token,),
        ).fetchone()

        if existing:

            conn.execute(
                """
                UPDATE device_tokens

                SET
                    user_id = ?,
                    platform = ?,
                    updated_at = ?

                WHERE token = ?
                """,
                (
                    data.user_id,
                    data.platform.strip(),
                    now_text(),
                    token,
                ),
            )

        else:

            conn.execute(
                """
                INSERT INTO device_tokens (
                    user_id,
                    token,
                    platform,
                    updated_at
                )

                VALUES (?, ?, ?, ?)
                """,
                (
                    data.user_id,
                    token,
                    data.platform.strip(),
                    now_text(),
                ),
            )

        return {
            "success": True,
            "message": "기기 토큰 저장 완료",
        }


# =========================================================
# 시작
# =========================================================

@app.on_event("startup")
def startup_event():
    init_db()
