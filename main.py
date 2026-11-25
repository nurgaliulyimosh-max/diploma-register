import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from pwdlib import PasswordHash
from sqlalchemy import ForeignKey, LargeBinary, create_engine, func, inspect, select, update
from starlette.datastructures import UploadFile
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

ROOT = Path(__file__).resolve().parent
KEY = os.getenv("DIPLOMA_SECRET") or secrets.token_hex(32)
engine = create_engine(os.getenv("DIPLOMA_DB_URL") or f"sqlite:///{ROOT / 'diploma.db'}")
passwords = PasswordHash.recommended()
app = FastAPI(title="Дипломдық жобаларды тіркеу")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
views = Jinja2Templates(directory=ROOT / "templates")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    password: Mapped[str]
    role: Mapped[str]


class Area(Base):
    __tablename__ = "areas"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)


class Topic(Base):
    __tablename__ = "topics"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    area: Mapped[str]
    description: Mapped[str]
    capacity: Mapped[int]
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    owner: Mapped[User] = relationship()


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str]
    motivation: Mapped[str]
    topic: Mapped[Topic] = relationship()
    student: Mapped[User] = relationship()
    events: Mapped[list["Event"]] = relationship(order_by="Event.id")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    status: Mapped[str]
    actor: Mapped[str]
    note: Mapped[str]
    created: Mapped[str]


class SavedTopic(Base):
    __tablename__ = "saved_topics"
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), primary_key=True)
    topic: Mapped[Topic] = relationship()


class Milestone(Base):
    __tablename__ = "milestones"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    title: Mapped[str]
    instructions: Mapped[str]
    due: Mapped[date]
    drafts: Mapped[list["Draft"]] = relationship(order_by="Draft.id")


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestones.id"), index=True)
    filename: Mapped[str]
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    note: Mapped[str]
    uploaded: Mapped[str]
    state: Mapped[str]
    feedback: Mapped[str] = mapped_column(default="")
    reviewed: Mapped[str] = mapped_column(default="")


DEFAULT_AREAS = ("Веб-әзірлеу", "Деректерді талдау", "Мобильді әзірлеу", "Ақпараттық қауіпсіздік")
DEMO_ENABLED = os.getenv("DIPLOMA_DEMO", "1") == "1"
ROLES = {"student": "Студент", "supervisor": "Ғылыми жетекші", "department": "Кафедра"}
STATUS = {"pending": "Жетекшіде", "agreed": "Кафедрада", "approved": "Бекітілді",
          "returned": "Түзетуге қайтарылды", "rejected": "Қабылданбады", "withdrawn": "Қайтарып алынды"}
ACTIVE = ("pending", "agreed", "approved", "returned")
RESERVED = ("agreed", "approved")
DRAFT_STATES = {"pending": "Тексеруде", "changes": "Түзету қажет", "accepted": "Қабылданды"}
MAX_DRAFT_BYTES = 10 * 1024 * 1024


def initialize():
    with Session(engine) as db:
        connection = db.connection()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        migrate_areas = not inspect(connection).has_table("areas")
        Base.metadata.create_all(connection)
        if migrate_areas:
            names = set(DEFAULT_AREAS).union(db.scalars(select(Topic.area).distinct()))
            db.add_all(Area(name=name) for name in sorted(names))
        fresh = not db.scalar(select(User.id).limit(1))
        if not DEMO_ENABLED:
            db.commit()
            return
        accounts = {}
        for name, email, role in (
            ("Аружан Сәрсен", "student@demo.kz", "student"),
            ("Айдана Нұрланқызы", "supervisor@demo.kz", "supervisor"),
            ("Марат Әбілов", "marat@demo.kz", "supervisor"),
            ("Кафедра үйлестірушісі", "department@demo.kz", "department"),
        ):
            user = db.scalar(select(User).where(User.email == email))
            if not user:
                user = User(name=name, email=email, role=role, password=passwords.hash("Demo2026!"))
                db.add(user)
            accounts[email] = user
        if not fresh or not migrate_areas:
            db.commit()
            return
        supervisor = accounts["supervisor@demo.kz"]
        other = accounts["marat@demo.kz"]
        rows = [
            ("Университет кітапханасындағы бос орындарды бақылау", 0, "Студенттер оқу залдарының жүктемесін көріп, бос орынды алдын ала таңдай алады."),
            ("Қалалық ауа сапасын талдау және болжау", 1, "Ашық мониторинг деректерінен тәуліктік өзгерістерді анықтап, болжамды интерактивті графикте көрсету."),
            ("Кампус ішінде қолжетімді маршруттарды жоспарлау", 2, "Ғимараттар арасындағы кедергісіз бағыттарды картаға түсіріп, маршрут құру."),
            ("Оқу порталындағы кіру оқиғаларын талдау", 3, "Сынақ журналы арқылы күмәнді кіру оқиғаларын анықтайтын бақылау тақтасын жасау."),
            ("Студенттік ұйымдардың іс-шараларын басқару", 0, "Ұйымдарға іс-шара жариялау, қатысушыларды тіркеу және қатысу есебін жүргізу мүмкіндігін беру."),
            ("Қоғамдық көлік жүктемесін зерттеу", 1, "Аялдама бойынша жолаушылар ағынын талдап, ең көп жүктеме түсетін уақытты анықтау."),
            ("Зертхана жабдығын брондау жүйесі", 0, "Жабдықтың қолжетімділігін көрсету және уақыт бойынша брондауды келісу."),
            ("Оқу материалдарын офлайн сақтау қолданбасы", 2, "Пән материалдарын жүктеп алып, интернетсіз оқуға және белгі қоюға мүмкіндік беру."),
        ]
        for index, (title, area, description) in enumerate(rows):
            db.add(Topic(title=title, area=DEFAULT_AREAS[area], description=description,
                         capacity=1 + index % 2, owner=supervisor if index % 3 != 2 else other))
        db.commit()


initialize()


def identity(request, db):
    try:
        claims = jwt.decode(request.cookies.get("session", ""), KEY, algorithms=["HS256"])
        return db.get(User, int(claims["sub"]))
    except (jwt.InvalidTokenError, ValueError, KeyError):
        return None


def require(request, db, *roles):
    user = identity(request, db)
    if not user or (roles and user.role not in roles):
        raise HTTPException(403, "Бұл әрекетке рұқсат жоқ.")
    return user


def project_access(request, db, application_id):
    user = require(request, db)
    entry = db.get(Application, application_id)
    if (not entry or entry.status != "approved" or
            user.id not in (entry.student_id, entry.topic.owner_id) and user.role != "department"):
        raise HTTPException(404, "Жоба табылмады.")
    return user, entry


def project_milestone(db, application_id, milestone_id):
    item = db.get(Milestone, milestone_id)
    if not item or item.application_id != application_id:
        raise HTTPException(404, "Кезең табылмады.")
    return item


def project_timestamp():
    return datetime.now(timezone(timedelta(hours=5))).strftime("%d.%m.%Y %H:%M")


@app.get("/projects/{application_id:int}")
def project(request: Request, application_id: int):
    with Session(engine) as db:
        user, entry = project_access(request, db, application_id)
        plan = db.scalars(select(Milestone).where(Milestone.application_id == application_id)
                          .order_by(Milestone.due, Milestone.id)).all()
        completed = sum(bool(item.drafts and item.drafts[-1].state == "accepted") for item in plan)
        return render(request, db, "project", entry=entry, plan=plan, completed=completed,
                      draft_states=DRAFT_STATES, is_supervisor=user.id == entry.topic.owner_id,
                      is_student=user.id == entry.student_id,
                      today=datetime.now(timezone(timedelta(hours=5))).date())


@app.post("/projects/{application_id:int}/plan")
async def create_milestone(request: Request, application_id: int):
    form = await request.form()
    title = str(form.get("title", "")).strip()
    instructions = str(form.get("instructions", "")).strip()
    path = f"/projects/{application_id}"
    with Session(engine) as db:
        user, entry = project_access(request, db, application_id)
        if user.id != entry.topic.owner_id:
            raise HTTPException(403)
        try:
            due = date.fromisoformat(str(form.get("due", "")))
        except ValueError:
            return redirect(path, "Мерзімді дұрыс көрсетіңіз.")
        if not 3 <= len(title) <= 120 or len(instructions) > 1500:
            return redirect(path, "Кезең атауы 3–120 таңбадан, тапсырма мәтіні ең көбі 1500 таңбадан тұруы керек.")
        db.add(Milestone(application_id=application_id, title=title, instructions=instructions, due=due))
        db.commit()
    return redirect(path, "Кезең жоспарға қосылды.")


@app.post("/projects/{application_id:int}/plan/{milestone_id:int}/draft")
async def submit_draft(request: Request, application_id: int, milestone_id: int):
    path = f"/projects/{application_id}"
    limit = request.headers.get("content-length", "")
    if limit.isdecimal() and int(limit) > MAX_DRAFT_BYTES + 65536:
        return redirect(path, "Файл көлемі 10 МБ-тан аспауы керек.")
    with Session(engine) as db:
        user, entry = project_access(request, db, application_id)
        if user.id != entry.student_id:
            raise HTTPException(403)
    async with request.form(max_files=1, max_fields=2, max_part_size=8192) as form:
        file = form.get("file")
        note = str(form.get("note", "")).strip()
        filename = re.split(r"[/\\]", file.filename or "")[-1] if isinstance(file, UploadFile) else ""
        if (not 1 <= len(filename) <= 180 or any(ord(char) < 32 for char in filename)
                or Path(filename).suffix.lower() not in (".pdf", ".docx") or len(note) > 1000):
            return redirect(path, "PDF немесе DOCX файлын таңдаңыз. Түсіндірме 1000 таңбадан аспауы керек.")
        content = await file.read(MAX_DRAFT_BYTES + 1)
    if not content or len(content) > MAX_DRAFT_BYTES:
        return redirect(path, "Файл бос болмауы және 10 МБ-тан аспауы керек.")
    with Session(engine) as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        item = project_milestone(db, application_id, milestone_id)
        if item.drafts and item.drafts[-1].state != "changes":
            return redirect(path, "Жаңа нұсқа түзету сұралғаннан кейін жіберіледі.")
        db.add(Draft(milestone_id=item.id, filename=filename, content=content,
                     note=note, uploaded=project_timestamp(), state="pending"))
        db.commit()
    return redirect(path, "Жұмыс нұсқасы жетекшіге жіберілді.")


@app.get("/projects/{application_id:int}/drafts/{draft_id:int}")
def download_draft(request: Request, application_id: int, draft_id: int):
    from urllib.parse import quote
    with Session(engine) as db:
        project_access(request, db, application_id)
        draft = db.get(Draft, draft_id)
        if not draft:
            raise HTTPException(404)
        project_milestone(db, application_id, draft.milestone_id)
        return Response(draft.content, media_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(draft.filename)}",
                                 "Cache-Control": "no-store"})


@app.post("/projects/{application_id:int}/drafts/{draft_id:int}")
async def review_draft(request: Request, application_id: int, draft_id: int):
    form = await request.form()
    decision = str(form.get("decision", ""))
    feedback = str(form.get("feedback", "")).strip()
    path = f"/projects/{application_id}"
    with Session(engine) as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        user, entry = project_access(request, db, application_id)
        if user.id != entry.topic.owner_id:
            raise HTTPException(403)
        draft = db.get(Draft, draft_id)
        if not draft:
            raise HTTPException(404)
        project_milestone(db, application_id, draft.milestone_id)
        if draft.state != "pending":
            return redirect(path, "Бұл нұсқа тексеріліп қойған.")
        if decision not in ("accepted", "changes") or len(feedback) > 1500 or (decision == "changes" and not feedback):
            return redirect(path, "Түзету сұралса, ескертпе жазыңыз (ең көбі 1500 таңба).")
        draft.state, draft.feedback, draft.reviewed = decision, feedback, project_timestamp()
        db.commit()
    return redirect(path, "Шешім сақталды.")


def issue_token(user):
    expiry = datetime.now(timezone.utc) + timedelta(hours=8)
    return jwt.encode({"sub": str(user.id), "exp": expiry}, KEY, algorithm="HS256")


def render(request, db, screen, **data):
    return views.TemplateResponse(request=request, name="site.html", context={
        "screen": screen, "user": identity(request, db),
        "areas": db.scalars(select(Area.name).order_by(Area.name)).all(), "roles": ROLES,
        "demo_enabled": DEMO_ENABLED,
        "statuses": STATUS, "message": request.query_params.get("message", ""), **data,
    })


def redirect(path, message=""):
    from urllib.parse import urlencode
    return RedirectResponse(path + ("?" + urlencode({"message": message}) if message else ""), 303)


def record(db, application, user, status, note=""):
    application.status = status
    db.flush()
    db.add(Event(application_id=application.id, status=status, actor=user.name, note=note,
                 created=datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")))


def availability(db):
    return dict(db.execute(select(Application.topic_id, func.count()).where(
        Application.status.in_(RESERVED)).group_by(Application.topic_id)).all())


def topic_or_404(db, topic_id):
    topic = db.get(Topic, topic_id)
    if not topic:
        raise HTTPException(404, "Тақырып табылмады.")
    return topic


@app.middleware("http")
async def origin_guard(request, call_next):
    if request.method == "POST":
        from urllib.parse import urlsplit
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.url.netloc:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("Forbidden", 403)
    return await call_next(request)


@app.get("/")
def catalog(request: Request, q: str = "", area: str = "", free: bool = False,
            page: int = 1, supervisor: str = ""):
    with Session(engine) as db:
        used = availability(db)
        all_topics = db.scalars(select(Topic).order_by(Topic.id)).all()
        supervisors = sorted({item.owner_id: item.owner for item in all_topics}.values(),
                             key=lambda person: person.name.casefold())
        matched = filter_topics((item for item in all_topics
                                 if not supervisor or str(item.owner_id) == supervisor), used, q, area, free)
        pages = max(1, (len(matched) + 5) // 6)
        page = min(max(1, page), pages)
        return render(request, db, "catalog", topics=matched[(page - 1) * 6:page * 6],
                      total=len(all_topics), supervisors=supervisors, supervisor=supervisor,
                      matched=len(matched), used=used, query=q, area=area, free=free, page=page, pages=pages)


def filter_topics(topics, used, q, area, free):
    query = q.strip().casefold()
    return [item for item in topics if
            (not query or query in f"{item.title} {item.owner.name} {item.description}".casefold())
            and (not area or item.area == area)
            and (not free or item.capacity > used.get(item.id, 0))]


@app.get("/topics/{topic_id:int}")
def topic_detail(request: Request, topic_id: int):
    with Session(engine) as db:
        topic = topic_or_404(db, topic_id)
        user = identity(request, db)
        saved = bool(user and db.get(SavedTopic, (user.id, topic_id)))
        return render(request, db, "detail", topic=topic, saved=saved,
                      slots=topic.capacity - availability(db).get(topic.id, 0))


@app.get("/saved")
def saved_topics(request: Request):
    with Session(engine) as db:
        user = require(request, db, "student")
        entries = db.scalars(select(SavedTopic).where(SavedTopic.student_id == user.id)
                             .order_by(SavedTopic.topic_id)).all()
        return render(request, db, "saved", topics=[entry.topic for entry in entries], used=availability(db))


@app.post("/saved/{topic_id:int}/{action}")
def save_selection(request: Request, topic_id: int, action: str):
    with Session(engine) as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        user = require(request, db, "student")
        topic_or_404(db, topic_id)
        entry = db.get(SavedTopic, (user.id, topic_id))
        if action == "add":
            if not entry:
                db.add(SavedTopic(student_id=user.id, topic_id=topic_id))
        elif action == "remove":
            if entry:
                db.delete(entry)
        else:
            raise HTTPException(404)
        db.commit()
        return redirect(f"/topics/{topic_id}" if action == "add" else "/saved")


@app.get("/account")
def account(request: Request, mode: str = "login"):
    with Session(engine) as db:
        return render(request, db, "account", mode=mode)


@app.get("/guide")
def guide(request: Request):
    with Session(engine) as db:
        return render(request, db, "guide")


@app.get("/api-center", include_in_schema=False)
def api_center(request: Request):
    with Session(engine) as db:
        return render(request, db, "api_center")


@app.post("/account")
async def sign_in(request: Request):
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == email))
        if form.get("mode") == "register":
            name = str(form.get("name", "")).strip()
            if user or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 150 or not 2 <= len(name) <= 100 or not 8 <= len(password) <= 128:
                return render(request, db, "account", mode="register", error="Тіркеу деректерін тексеріңіз. Құпиясөз 8–128 таңбадан тұруы керек.")
            user = User(email=email, name=name, password=passwords.hash(password), role="student")
            db.add(user)
            from sqlalchemy.exc import IntegrityError
            try:
                db.commit()
            except IntegrityError:
                return render(request, db, "account", mode="register", error="Бұл пошта тіркелген.")
        elif not user or len(password) > 128 or not passwords.verify(password, user.password):
            return render(request, db, "account", mode="login", error="Пошта немесе құпиясөз қате.")
        destination = str(form.get("next", ""))
        response = redirect(destination if re.fullmatch(r"/topics/[0-9]+", destination) else "/")
        response.set_cookie("session", issue_token(user), httponly=True, samesite="lax", max_age=28800)
        return response


@app.post("/logout")
def logout():
    response = redirect("/")
    response.delete_cookie("session")
    return response


@app.get("/applications")
def applications(request: Request, review: str = "", selected: int | None = None):
    with Session(engine) as db:
        user = identity(request, db)
        if not user:
            return redirect("/account")
        statement = select(Application).order_by(Application.id.desc())
        if user.role == "student":
            statement = statement.where(Application.student_id == user.id)
        if user.role == "supervisor":
            statement = statement.join(Topic).where(Topic.owner_id == user.id)
        if user.role == "department":
            statement = statement.where(Application.status != "pending")
        entries = db.scalars(statement).all()
        pending_status = {"supervisor": "pending", "department": "agreed"}.get(user.role)
        waiting = sum(item.status == pending_status for item in entries)
        if pending_status:
            review = review if review in ("waiting", "all") else ("waiting" if waiting else "all")
            if review == "waiting":
                entries = [item for item in entries if item.status == pending_status]
        entry = next((item for item in entries if item.id == selected), entries[0] if entries else None)
        return render(request, db, "applications", queue=entries, entries=[entry] if entry else [],
                      waiting=waiting, review=review)


@app.post("/topics/{topic_id:int}/apply")
async def apply(request: Request, topic_id: int):
    form = await request.form()
    motivation = str(form.get("motivation", "")).strip()
    with Session(engine) as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        user = require(request, db, "student")
        topic = topic_or_404(db, topic_id)
        if not 10 <= len(motivation) <= 1500:
            return redirect(f"/topics/{topic_id}", "Негіздеме 10–1500 таңбадан тұруы керек.")
        if availability(db).get(topic_id, 0) >= topic.capacity:
            return redirect(f"/topics/{topic_id}", "Бұл тақырыпта бос орын қалмады.")
        current = db.scalar(select(Application).where(
            Application.student_id == user.id, Application.status.in_(ACTIVE)))
        if current and (current.status != "returned" or current.topic_id != topic_id):
            return redirect("/applications", "Бір уақытта бір белсенді өтінім беруге болады.")
        if current:
            current.motivation = motivation
        else:
            current = Application(topic_id=topic_id, student_id=user.id, motivation=motivation, status="pending")
            db.add(current)
        record(db, current, user, "pending", motivation)
        db.commit()
        return redirect("/applications", "Өтінім жетекшіге жіберілді.")


@app.post("/applications/{application_id:int}")
async def decide(request: Request, application_id: int):
    form = await request.form()
    action = str(form.get("action", ""))
    note = str(form.get("note", "")).strip()
    with Session(engine) as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        user = require(request, db)
        entry = db.get(Application, application_id)
        if not entry:
            raise HTTPException(404)
        allowed = set()
        if user.id == entry.student_id and entry.status in ("pending", "returned", "agreed"):
            allowed = {"withdrawn"}
        if user.id == entry.topic.owner_id and entry.status == "pending":
            allowed = {"agreed", "returned", "rejected"}
        if user.role == "department" and entry.status == "agreed":
            allowed = {"approved", "returned", "rejected"}
        if action not in allowed:
            raise HTTPException(403, "Бұл шешімге рұқсат жоқ немесе өтінім күйі өзгерді.")
        if len(note) > 1000 or (action in ("returned", "rejected") and not note):
            return redirect("/applications", "Шешімнің себебін көрсетіңіз (ең көбі 1000 таңба).")
        if action == "agreed" and availability(db).get(entry.topic_id, 0) >= entry.topic.capacity:
            return redirect("/applications", "Бос орын қалмады.")
        record(db, entry, user, action, note)
        db.commit()
        return redirect("/applications", "Шешім сақталды.")


@app.get("/manage")
def manage(request: Request):
    with Session(engine) as db:
        user = require(request, db, "supervisor", "department")
        statement = select(Topic).order_by(Topic.id)
        if user.role == "supervisor":
            statement = statement.where(Topic.owner_id == user.id)
        return render(request, db, "manage", topics=db.scalars(statement).all())


@app.get("/areas")
def manage_areas(request: Request):
    with Session(engine) as db:
        require(request, db, "department")
        counts = dict(db.execute(select(Topic.area, func.count()).group_by(Topic.area)).all())
        return render(request, db, "areas", directions=db.scalars(select(Area).order_by(Area.name)).all(),
                      counts=counts)


@app.post("/areas")
@app.post("/areas/{area_id:int}")
async def change_area(request: Request, area_id: int | None = None):
    form = await request.form()
    name = " ".join(str(form.get("name", "")).split())
    with Session(engine) as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        require(request, db, "department")
        area = db.get(Area, area_id) if area_id is not None else None
        if area_id is not None and area is None:
            raise HTTPException(404)
        if area and form.get("action") == "delete":
            if db.scalar(select(Topic.id).where(Topic.area == area.name).limit(1)):
                return redirect("/areas", "Тақырыптар бар бағытты жою үшін алдымен тақырыптардың бағытын өзгертіңіз.")
            db.delete(area)
        else:
            if not 2 <= len(name) <= 80:
                return redirect("/areas", "Бағыт атауы 2–80 таңбадан тұруы керек.")
            if any(item.id != area_id and item.name.casefold() == name.casefold()
                   for item in db.scalars(select(Area))):
                return redirect("/areas", "Бұл бағыт тіркелген.")
            if area:
                db.execute(update(Topic).where(Topic.area == area.name).values(area=name))
                area.name = name
            else:
                db.add(Area(name=name))
        db.commit()
    return redirect("/areas", "Өзгеріс сақталды.")


def can_edit(user, topic):
    if user.role != "department" and topic.owner_id != user.id:
        raise HTTPException(403)


@app.get("/edit")
@app.get("/edit/{topic_id:int}")
def edit(request: Request, topic_id: int = 0):
    with Session(engine) as db:
        user = require(request, db, "supervisor", "department")
        topic = topic_or_404(db, topic_id) if topic_id else None
        if topic:
            can_edit(user, topic)
        return render(request, db, "edit", topic=topic, data=topic, related=[],
                      supervisors=db.scalars(select(User).where(User.role == "supervisor")).all())


def normalized(title):
    return " ".join(re.findall(r"\w+", title.casefold()))


def title_matches(db, topic_id, title):
    normalized_title = normalized(title)
    matches = []
    for candidate in db.scalars(select(Topic).where(Topic.id != topic_id)):
        ratio = SequenceMatcher(None, normalized_title, normalized(candidate.title)).ratio()
        if ratio >= 0.72:
            matches.append((candidate, ratio))
    return matches


def remove_topic(db, topic):
    if db.scalar(select(Application.id).where(Application.topic_id == topic.id).limit(1)):
        raise HTTPException(409, "Өтінім тарихы бар тақырыпты жоюға болмайды.")
    from sqlalchemy import delete
    db.execute(delete(SavedTopic).where(SavedTopic.topic_id == topic.id))
    db.delete(topic)


@app.post("/edit")
@app.post("/edit/{topic_id:int}")
async def save(request: Request, topic_id: int = 0):
    form = await request.form()
    title = str(form.get("title", "")).strip()
    description = str(form.get("description", "")).strip()
    area = str(form.get("area", ""))
    capacity = str(form.get("capacity", ""))
    owner_id = str(form.get("owner_id", ""))
    with Session(engine) as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        user = require(request, db, "supervisor", "department")
        topic = topic_or_404(db, topic_id) if topic_id else None
        if topic:
            can_edit(user, topic)
        if form.get("delete") == "yes" and topic:
            try:
                remove_topic(db, topic)
            except HTTPException as exc:
                return redirect("/manage", exc.detail)
            db.commit()
            return redirect("/manage", "Тақырып жойылды.")
        supervisors = db.scalars(select(User).where(User.role == "supervisor")).all()
        owner = topic.owner if topic else next((item for item in supervisors if str(item.id) == owner_id), None)
        if user.role == "supervisor":
            owner = user
        data = {"title": title, "description": description, "area": area, "capacity": capacity,
                "owner_id": str(owner.id) if owner else owner_id}
        error = ""
        related = []
        if not (8 <= len(title) <= 180 and normalized(title) and 20 <= len(description) <= 2000
                and db.scalar(select(Area.id).where(Area.name == area))
                and re.fullmatch(r"[1-9]|1[0-9]|20", capacity) and owner):
            error = "Өрістерді тексеріңіз. Орын саны 1–20 аралығында болуы керек."
        elif int(capacity) < availability(db).get(topic_id, 0):
            error = "Орын саны келісілген өтінімдер санынан аз болмауы керек."
        elif not topic or normalized(title) != normalized(topic.title):
            matches = title_matches(db, topic_id, title)
            related = [candidate for candidate, _ in matches]
            if any(ratio == 1 for _, ratio in matches):
                error = "Осындай атауы бар тақырып тіркелген."
        if error or (related and form.get("confirm") != "yes"):
            return render(request, db, "edit", topic=topic, data=data, related=related,
                          supervisors=supervisors, error=error)
        if not topic:
            topic = Topic(owner=owner)
            db.add(topic)
        topic.title, topic.description, topic.area, topic.capacity = title, description, area, int(capacity)
        db.commit()
        return redirect("/manage", "Тақырып сақталды.")


class TopicCreate(BaseModel):
    title: str = Field(min_length=8, max_length=180)
    description: str = Field(min_length=20, max_length=2000)
    area: str = Field(min_length=2, max_length=80)
    capacity: int = Field(ge=1, le=20)
    owner_id: int | None = None
    confirm_similar: bool = False

    @field_validator("title", "description", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("title")
    @classmethod
    def meaningful_title(cls, value):
        if not normalized(value):
            raise ValueError("Тақырып атауында әріп немесе сан болуы керек.")
        return value

class TopicRead(BaseModel):
    id: int
    title: str
    description: str
    area: str
    capacity: int
    free_slots: int
    supervisor: str


class TopicPage(BaseModel):
    items: list[TopicRead]
    page: int
    total: int


oauth2 = OAuth2PasswordBearer(tokenUrl="/api/token")


def api_editor(token: str = Depends(oauth2)):
    try:
        claims = jwt.decode(token, KEY, algorithms=["HS256"])
        with Session(engine) as db:
            user = db.get(User, int(claims["sub"]))
    except (jwt.InvalidTokenError, ValueError, KeyError):
        user = None
    if not user:
        raise HTTPException(401, "Сеанс жарамсыз.", headers={"WWW-Authenticate": "Bearer"})
    if user.role not in ("supervisor", "department"):
        raise HTTPException(403, "Тақырыпты басқаруға рұқсат жоқ.")
    return user


@app.post("/api/token", tags=["Authentication"])
def api_token(form: OAuth2PasswordRequestForm = Depends()):
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == form.username.strip().lower()))
        if not user or len(form.password) > 128 or not passwords.verify(form.password, user.password):
            raise HTTPException(401, "Пошта немесе құпиясөз қате.", headers={"WWW-Authenticate": "Bearer"})
        return {"access_token": issue_token(user), "token_type": "bearer"}


def topic_read(topic, used):
    return TopicRead(id=topic.id, title=topic.title, description=topic.description, area=topic.area,
                     capacity=topic.capacity, free_slots=topic.capacity - used.get(topic.id, 0),
                     supervisor=topic.owner.name)


@app.get("/api/topics", response_model=TopicPage, tags=["Topics"])
def api_topics(page: int = 1, area: str = "", q: str = "", free: bool = False):
    with Session(engine) as db:
        used = availability(db)
        matches = filter_topics(db.scalars(select(Topic).order_by(Topic.id)), used, q, area, free)
        page = max(1, page)
        return TopicPage(items=[topic_read(t, used) for t in matches[(page - 1) * 6:page * 6]],
                         page=page, total=len(matches))


@app.get("/api/topics/{topic_id}", response_model=TopicRead, tags=["Topics"])
def api_topic(topic_id: int):
    with Session(engine) as db:
        return topic_read(topic_or_404(db, topic_id), availability(db))


def api_save(data, user, topic_id=None):
    with Session(engine) as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if not db.scalar(select(Area.id).where(Area.name == data.area)):
            raise HTTPException(422, "Каталогта көрсетілген бағытты таңдаңыз.")
        topic = topic_or_404(db, topic_id) if topic_id is not None else None
        if topic:
            can_edit(user, topic)
        used = availability(db)
        if data.capacity < used.get(topic_id, 0):
            raise HTTPException(409, "Орын саны келісілген өтінімдер санынан аз.")
        if not topic or normalized(data.title) != normalized(topic.title):
            matches = title_matches(db, topic_id or 0, data.title)
            if any(ratio == 1 for _, ratio in matches):
                raise HTTPException(409, "Осындай атауы бар тақырып тіркелген.")
            if matches and not data.confirm_similar:
                raise HTTPException(409, {"similar_topics": [item.id for item, _ in matches]})
        if not topic:
            owner_id = user.id if user.role == "supervisor" else data.owner_id
            owner = db.get(User, owner_id) if owner_id is not None else None
            if not owner or owner.role != "supervisor":
                raise HTTPException(422, "Ғылыми жетекшіні көрсетіңіз.")
            topic = Topic(owner=owner)
            db.add(topic)
        topic.title, topic.description, topic.area, topic.capacity = data.title, data.description, data.area, data.capacity
        db.commit()
        return topic_read(topic, used)


@app.post("/api/topics", response_model=TopicRead, status_code=201, tags=["Topics"])
def api_create(data: TopicCreate, user: User = Depends(api_editor)):
    return api_save(data, user)


@app.put("/api/topics/{topic_id}", response_model=TopicRead, tags=["Topics"])
def api_update(topic_id: int, data: TopicCreate, user: User = Depends(api_editor)):
    return api_save(data, user, topic_id)


@app.delete("/api/topics/{topic_id}", status_code=204, tags=["Topics"])
def api_delete(topic_id: int, user: User = Depends(api_editor)):
    with Session(engine) as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        topic = topic_or_404(db, topic_id)
        can_edit(user, topic)
        remove_topic(db, topic)
        db.commit()
