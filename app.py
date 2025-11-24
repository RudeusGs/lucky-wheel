import os
import random
from datetime import date, timedelta

from flask import (
    Flask,
    jsonify,
    request,
    send_from_directory,
    session,
    redirect,
)
from flask_cors import CORS

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Boolean,
)
from sqlalchemy.orm import sessionmaker, declarative_base, scoped_session

from dotenv import load_dotenv

# =========================
# LOAD .ENV & CẤU HÌNH DB, SECRET
# =========================

load_dotenv()  # đọc file .env nếu có

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL chưa được cấu hình! Kiểm tra lại file .env hoặc biến môi trường."
    )

engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

# Nếu dùng Neon (Postgres) thì không cần connect_args, nhưng có thể thêm pool_pre_ping
engine = create_engine(DATABASE_URL, pool_pre_ping=True, **engine_kwargs)

SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
Base = declarative_base()

# Flask secret key để dùng session (login người chơi & admin)
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")

# Admin key (để login admin)
ADMIN_KEY = os.getenv("LUCKY_WHEEL_ADMIN_KEY", "changeme_admin_key")


# =========================
# MODEL
# =========================

class Prize(Base):
    __tablename__ = "prizes"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    weight = Column(Integer, nullable=False, default=1)
    active = Column(Boolean, nullable=False, default=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "weight": self.weight,
            "active": self.active,
        }


class Player(Base):
    """
    Người chơi – dùng để giới hạn lượt quay
    - spins_per_day: cho phép quay tối đa mấy lần 1 ngày (ở đây sẽ để 1)
    - last_spin_date: ngày gần nhất quay
    - spins_today: số lần đã quay trong ngày đó
    """
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(100), nullable=True)
    spins_per_day = Column(Integer, nullable=False, default=1)
    last_spin_date = Column(String(10), nullable=True)  # lưu "YYYY-MM-DD" cho đơn giản
    spins_today = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)

    def spins_left_today(self) -> int:
        today_str = date.today().isoformat()
        if self.last_spin_date != today_str:
            # ngày mới → reset về full lượt
            return self.spins_per_day
        return max(self.spins_per_day - self.spins_today, 0)


# =========================
# INIT DB
# =========================

def init_db():
    """Tạo bảng và seed dữ liệu mặc định nếu trống."""
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        # Seed prize nếu chưa có
        if db.query(Prize).count() == 0:
            defaults = [
                ("100K", 10, True),
                ("Mũi Tên", 8, True),
                ("1M", 1, True),
                ("Voucher", 6, True),
                ("500K", 3, True),
                ("Quà Tặng", 7, True),
                ("Không Có", 50, True),
                ("200K", 15, True),
            ]
            for name, weight, active in defaults:
                db.add(Prize(name=name, weight=weight, active=active))

        # Seed player nếu chưa có (ví dụ tạo 100 tài khoản)
        if db.query(Player).count() == 0:
            for i in range(1, 101):
                db.add(Player(
                    name=f"User #{i}",
                    spins_per_day=1,
                    last_spin_date=None,
                    spins_today=0,
                    active=True,
                ))

        db.commit()


def weighted_random_choice(prizes):
    """prizes: list[Prize] – random theo weight."""
    weights = [max(int(p.weight or 0), 0) for p in prizes]
    total = sum(weights)
    if total <= 0:
        return None
    r = random.uniform(0, total)
    upto = 0
    for p, w in zip(prizes, weights):
        upto += w
        if r <= upto:
            return p
    return prizes[-1]


# =========================
# FLASK APP
# =========================

app = Flask(__name__, static_folder=".", static_url_path="")
app.secret_key = SECRET_KEY
CORS(app)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=1)
app.config["SESSION_COOKIE_NAME"] = "luckywheel_session"

@app.before_request
def make_session_permanent():
    session.permanent = True

# =========================
# ADMIN HELPER
# =========================

def is_admin():
    return session.get("is_admin") is True


def check_admin():
    """
    - Nếu đã login admin qua session → OK
    - Hoặc có gửi đúng X-Admin-Key / ?admin_key (cho Postman) → OK
    - Ngược lại → trả JSON 401
    """
    if is_admin():
        return None

    key = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if key and key == ADMIN_KEY:
        return None

    # Trả JSON, không dùng abort để tránh HTML
    return jsonify({"success": False, "error": "Unauthorized: admin login required"}), 401


# =========================
# ROUTES TĨNH: TRANG HTML
# =========================

@app.route("/")
def serve_index():
    # Trang vòng quay
    return send_from_directory(".", "index.html")


@app.route("/admin")
def admin_page():
    # Nếu chưa login admin -> đá về trang login
    if not is_admin():
        return redirect("/admin/login")
    return send_from_directory(".", "admin.html")


@app.route("/admin/login")
def admin_login_page():
    # Nếu đã login admin rồi -> vào thẳng /admin luôn
    if is_admin():
        return redirect("/admin")
    return send_from_directory(".", "login.html")


# =========================
# LOGIN NGƯỜI CHƠI BẰNG QR
# =========================

@app.route("/auto-login")
def auto_login():
    today_str = date.today().isoformat()

    with SessionLocal() as db:
        pid = session.get("player_id")
        if pid:
            player = db.query(Player).filter(
                Player.id == pid,
                Player.active == True
            ).first()

            if player:
                if player.last_spin_date != today_str:
                    player.last_spin_date = today_str
                    player.spins_today = 0
                    db.commit()
                if player.spins_today < player.spins_per_day:
                    return redirect("/")
                return """
                <script>
                    alert("Hôm nay bạn đã quay đủ số lần trên thiết bị này, vui lòng quay lại ngày mai.");
                    window.location.href = "/";
                </script>
                """, 200
            session.pop("player_id", None)
        players = db.query(Player).filter(Player.active == True).all()

        available = []
        for p in players:
            if p.last_spin_date != today_str:
                available.append(p)
            else:
                if p.spins_today < p.spins_per_day:
                    available.append(p)

        if not available:
            return "Hôm nay đã hết tài khoản còn lượt quay, vui lòng quay lại ngày mai.", 200

        chosen = random.choice(available)
        session["player_id"] = chosen.id

        return redirect("/")



@app.route("/api/player/status", methods=["GET"])
def player_status():
    """
    Cho frontend biết:
    - đã login chưa
    - còn bao nhiêu lượt quay hôm nay
    """
    pid = session.get("player_id")
    if not pid:
        return jsonify({
            "authenticated": False,
            "spins_left_today": 0,
            "player": None,
        })

    with SessionLocal() as db:
        player = db.query(Player).filter(Player.id == pid, Player.active == True).first()  # noqa
        if not player:
            session.pop("player_id", None)
            return jsonify({
                "authenticated": False,
                "spins_left_today": 0,
                "player": None,
            })

        spins_left = player.spins_left_today()
        return jsonify({
            "authenticated": True,
            "spins_left_today": spins_left,
            "player": {
                "id": player.id,
                "name": player.name,
            },
        })


# =========================
# API PRIZE & SPIN (PUBLIC)
# =========================

@app.route("/api/prizes", methods=["GET"])
def get_prizes():
    with SessionLocal() as db:
        q = db.query(Prize)
        if request.args.get("active_only") == "1":
            q = q.filter(Prize.active == True)  # noqa
        prizes = q.order_by(Prize.id).all()
        return jsonify({"prizes": [p.to_dict() for p in prizes]})


@app.route("/api/spin", methods=["POST"])
def spin():
    pid = session.get("player_id")
    if not pid:
        return jsonify({
            "success": False,
            "error": "Bạn chưa đăng nhập (hãy quét QR để chơi).",
        }), 401

    today_str = date.today().isoformat()

    with SessionLocal() as db:
        player = db.query(Player).filter(Player.id == pid, Player.active == True).first()  # noqa
        if not player:
            return jsonify({"success": False, "error": "Tài khoản không hợp lệ."}), 400

        if player.last_spin_date != today_str:
            player.last_spin_date = today_str
            player.spins_today = 0

        if player.spins_today >= player.spins_per_day:
            return jsonify({
                "success": False,
                "error": "Hôm nay bạn đã quay đủ số lần.",
            }), 400

        prizes = (
            db.query(Prize)
            .filter(Prize.active == True, Prize.weight > 0)  # noqa
            .order_by(Prize.id)
            .all()
        )
        if not prizes:
            return jsonify({"success": False, "error": "No active prizes"}), 400

        prize = weighted_random_choice(prizes)
        if prize is None:
            return jsonify({"success": False, "error": "Cannot choose prize"}), 500

        player.spins_today += 1
        db.commit()

        spins_left = max(player.spins_per_day - player.spins_today, 0)

        return jsonify({
            "success": True,
            "prize": prize.to_dict(),
            "spins_left_today": spins_left,
        })


# =========================
# ADMIN LOGIN (SESSION)
# =========================

@app.route("/api/admin/login", methods=["POST"])
def admin_login_api():
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("key") or "").strip()

    if not key:
        return jsonify({"success": False, "error": "Chưa nhập admin key."}), 400

    if key != ADMIN_KEY:
        session.pop("is_admin", None)
        return jsonify({"success": False, "error": "Sai admin key."}), 401

    session["is_admin"] = True
    return jsonify({"success": True})


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout_api():
    session.pop("is_admin", None)
    return jsonify({"success": True})


# =========================
# ADMIN API: CRUD PRIZE
# =========================

@app.route("/api/admin/prizes", methods=["POST"])
def admin_create_prize():
    resp = check_admin()
    if resp is not None:
        return resp

    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "name is required"}), 400
    weight = int(data.get("weight", 1))
    active = bool(data.get("active", True))

    with SessionLocal() as db:
        p = Prize(name=name, weight=weight, active=active)
        db.add(p)
        db.commit()
        db.refresh(p)
        return jsonify({"success": True, "prize": p.to_dict()}), 201


@app.route("/api/admin/prizes/<int:pid>", methods=["PATCH", "PUT"])
def admin_update_prize(pid):
    resp = check_admin()
    if resp is not None:
        return resp

    data = request.get_json(force=True, silent=True) or {}
    with SessionLocal() as db:
        p = db.query(Prize).filter(Prize.id == pid).first()
        if not p:
            return jsonify({"error": "Prize not found"}), 404
        if "name" in data:
            p.name = str(data["name"])
        if "weight" in data:
            p.weight = int(data["weight"])
        if "active" in data:
            p.active = bool(data["active"])
        db.commit()
        db.refresh(p)
        return jsonify({"success": True, "prize": p.to_dict()})


@app.route("/api/admin/prizes/<int:pid>", methods=["DELETE"])
def admin_delete_prize(pid):
    resp = check_admin()
    if resp is not None:
        return resp

    with SessionLocal() as db:
        p = db.query(Prize).filter(Prize.id == pid).first()
        if not p:
            return jsonify({"error": "Prize not found"}), 404
        db.delete(p)
        db.commit()
        return jsonify({"success": True})

        
@app.route("/qr")
def qr_page():
    return send_from_directory(".", "qr.html")

# =========================
# KHỞI TẠO DB KHI IMPORT (Render + gunicorn)
# =========================

init_db()
print(">>> USING DATABASE:", DATABASE_URL)


# =========================
# MAIN (chạy local)
# =========================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
