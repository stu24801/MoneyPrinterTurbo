"""Simple file-based authentication for the MoneyPrinterTurbo WebUI.

Users are stored in storage/users.json (volume-mounted, survives restarts):
{
  "settings": {"allow_register": false},
  "users": {
    "<username>": {"salt": "<hex>", "hash": "<hex>", "role": "admin"|"user", "created_at": "..."}
  }
}
Passwords are hashed with PBKDF2-HMAC-SHA256 (200k iterations, per-user salt).
"""

import hashlib
import json
import os
import secrets
import time
from datetime import datetime

import streamlit as st

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
USERS_FILE = os.path.join(ROOT_DIR, "storage", "users.json")

PBKDF2_ITERATIONS = 200_000


# ── storage ───────────────────────────────────────────────────────────────────
def _load_db() -> dict:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        db = {}
    db.setdefault("settings", {}).setdefault("allow_register", False)
    db.setdefault("users", {})
    return db


def _save_db(db: dict):
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)


# ── password hashing ──────────────────────────────────────────────────────────
def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    ).hex()


def _make_user(password: str, role: str) -> dict:
    salt = secrets.token_hex(16)
    return {
        "salt": salt,
        "hash": _hash_password(password, salt),
        "role": role,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }


def _verify(user: dict, password: str) -> bool:
    return secrets.compare_digest(user["hash"], _hash_password(password, user["salt"]))


# ── operations ────────────────────────────────────────────────────────────────
def create_user(username: str, password: str, role: str = "user") -> str:
    username = username.strip()
    if not username or len(username) < 2:
        return "使用者名稱至少 2 個字元"
    if len(password) < 8:
        return "密碼至少 8 個字元"
    db = _load_db()
    if username in db["users"]:
        return "使用者名稱已存在"
    db["users"][username] = _make_user(password, role)
    _save_db(db)
    return ""


def authenticate(username: str, password: str):
    db = _load_db()
    user = db["users"].get(username.strip())
    if user and _verify(user, password):
        return {"username": username.strip(), "role": user["role"]}
    return None


# ── UI: login / register gate ─────────────────────────────────────────────────
def require_login():
    """Render login (and optional register) UI and st.stop() until authenticated."""
    if st.session_state.get("auth_user"):
        return st.session_state["auth_user"]

    db = _load_db()
    allow_register = db["settings"].get("allow_register", False)

    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.title("🔐 登入")
        tabs = st.tabs(["登入", "註冊"] if allow_register else ["登入"])

        with tabs[0]:
            with st.form("login_form"):
                username = st.text_input("使用者名稱")
                password = st.text_input("密碼", type="password")
                submitted = st.form_submit_button("登入", use_container_width=True)
            if submitted:
                # small constant delay to slow down brute force
                time.sleep(1)
                auth = authenticate(username, password)
                if auth:
                    st.session_state["auth_user"] = auth
                    st.rerun()
                else:
                    st.error("使用者名稱或密碼錯誤")

        if allow_register:
            with tabs[1]:
                with st.form("register_form"):
                    r_username = st.text_input("使用者名稱", key="reg_u")
                    r_password = st.text_input("密碼（至少 8 碼）", type="password", key="reg_p")
                    r_password2 = st.text_input("確認密碼", type="password", key="reg_p2")
                    r_submitted = st.form_submit_button("註冊", use_container_width=True)
                if r_submitted:
                    if r_password != r_password2:
                        st.error("兩次密碼不一致")
                    else:
                        err = create_user(r_username, r_password, role="user")
                        if err:
                            st.error(err)
                        else:
                            st.success("註冊成功，請切回登入分頁登入")
    st.stop()


# ── UI: admin backend panel (sidebar) ─────────────────────────────────────────
def render_user_sidebar():
    """Sidebar: current user info + logout; full management panel for admins."""
    auth = st.session_state.get("auth_user")
    if not auth:
        return

    with st.sidebar:
        st.markdown(f"👤 **{auth['username']}**（{'管理員' if auth['role'] == 'admin' else '一般使用者'}）")
        if st.button("登出", use_container_width=True):
            st.session_state.pop("auth_user", None)
            st.rerun()

        # ── change own password (all users) ──
        with st.expander("🔑 修改我的密碼"):
            with st.form("self_pw_form"):
                old_pw = st.text_input("目前密碼", type="password")
                new_pw = st.text_input("新密碼（至少 8 碼）", type="password")
                ok = st.form_submit_button("更新密碼")
            if ok:
                db = _load_db()
                user = db["users"].get(auth["username"])
                if not user or not _verify(user, old_pw):
                    st.error("目前密碼錯誤")
                elif len(new_pw) < 8:
                    st.error("新密碼至少 8 個字元")
                else:
                    db["users"][auth["username"]] = {
                        **_make_user(new_pw, user["role"]),
                        "created_at": user["created_at"],
                    }
                    _save_db(db)
                    st.success("密碼已更新")

        if auth["role"] != "admin":
            return

        # ── admin backend ──
        st.divider()
        st.subheader("🛠️ 管理後台")
        db = _load_db()

        allow_register = st.toggle(
            "開放註冊", value=db["settings"].get("allow_register", False)
        )
        if allow_register != db["settings"].get("allow_register", False):
            db["settings"]["allow_register"] = allow_register
            _save_db(db)
            st.success("設定已儲存")

        with st.expander("👥 使用者管理", expanded=False):
            for name, info in list(db["users"].items()):
                cols = st.columns([2, 1, 1])
                cols[0].markdown(f"**{name}**  \n`{info['role']}`")
                if name != auth["username"]:
                    if cols[1].button("刪除", key=f"del_{name}"):
                        del db["users"][name]
                        _save_db(db)
                        st.rerun()
                new_pw = cols[2].text_input(
                    "新密碼", key=f"rpw_{name}", label_visibility="collapsed",
                    placeholder="重設密碼", type="password",
                )
                if new_pw:
                    if len(new_pw) < 8:
                        cols[2].error("至少 8 碼")
                    else:
                        db["users"][name] = {
                            **_make_user(new_pw, info["role"]),
                            "created_at": info["created_at"],
                        }
                        _save_db(db)
                        cols[2].success("已重設")

        with st.expander("➕ 新增使用者 / 變更帳號名稱"):
            st.caption("變更帳號名稱＝新增一個新帳號後，把舊帳號刪除")
            with st.form("add_user_form"):
                a_username = st.text_input("使用者名稱", key="adm_u")
                a_password = st.text_input("密碼（至少 8 碼）", type="password", key="adm_p")
                a_role = st.selectbox("角色", ["user", "admin"], key="adm_r")
                a_ok = st.form_submit_button("建立帳號")
            if a_ok:
                err = create_user(a_username, a_password, role=a_role)
                st.error(err) if err else st.success(f"已建立 {a_role} 帳號：{a_username}")
