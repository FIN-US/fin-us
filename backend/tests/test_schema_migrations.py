"""#162: SQLModel의 create_all()은 새 테이블은 만들지만 이미 존재하는 테이블에
컬럼을 추가하지 않는다 (SQLAlchemy 문서화된 동작, alembic 없는 이 repo에서는
database._run_schema_migrations()가 그 간극을 메운다). 여기서는 실제로 컬럼이
없는 "구버전" SQLite 파일을 직접 만든 뒤 새 코드가 그 파일에 대해 무엇을 하는지
검증한다 — 새로 만든 빈 DB만으로는 이 회귀를 잡을 수 없다.
"""
import sqlite3
import threading

from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table, create_engine

from backend import database


def _create_old_schema_agentreport(db_path: str) -> None:
    """provider_supports_tools 컬럼이 없던 시절(이 브랜치 이전 models.py)의
    agentreport 테이블을 그대로 재현한다 — created_at을 포함한 다른 컬럼은 이번
    변경으로 새로 생긴 것이 아니므로 그대로 둔다.

    다른 테이블(Portfolio, TradeHistory, Diary 등)은 실제 배포 환경처럼 이미
    최신 스키마로 존재한다고 가정하고 먼저 만들어 둔다 — 이 브랜치가 건드린 건
    agentreport 하나뿐이므로 나머지가 구버전일 이유가 없고, 만약 여기서 만들지
    않으면 동시성 테스트(test_init_db_concurrent_migration_does_not_abort_startup)
    에서 create_all() 자체가 agentreport 외의 테이블을 놓고 별개의
    "table already exists" 경합을 일으켜 우리가 실제로 검증하려는
    ALTER TABLE 경합(MEDIUM3)과 뒤섞인다.
    """
    from sqlmodel import SQLModel

    setup_engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(setup_engine)  # 최신 스키마로 다른 테이블들 생성

    old_metadata = MetaData()
    Table(
        "agentreport",
        old_metadata,
        Column("id", Integer, primary_key=True),
        Column("stock_code", String),
        Column("stock_name", String),
        Column("provider", String),
        Column("summary", String),
        Column("decision", String),
        Column("confidence_score", Float),
        Column("reason", String),
        Column("created_at", DateTime),
    )
    with setup_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE agentreport")  # 최신 스키마로 만들어진 것을 버리고
    old_metadata.create_all(setup_engine)  # 구버전 agentreport로 다시 만든다
    with setup_engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO agentreport "
            "(stock_code, stock_name, provider, summary, decision, confidence_score, reason, created_at) "
            "VALUES ('005930', '삼성전자', 'openai', '요약', 'BUY', 0.9, '지어낸 근거', "
            "'2026-01-01 00:00:00')"
        )
    setup_engine.dispose()


def test_create_all_does_not_add_column_to_existing_table(tmp_path):
    """전제 확인: create_all만으로는 기존 agentreport 테이블에
    provider_supports_tools가 생기지 않는다. 이 전제가 깨지면(SQLAlchemy 동작이
    바뀌면) 아래 마이그레이션 로직 자체가 불필요해지므로 먼저 명시적으로 확인한다.
    """
    db_path = tmp_path / "old_only_create_all.db"
    _create_old_schema_agentreport(str(db_path))

    from sqlmodel import SQLModel

    probe_engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(probe_engine)  # backend.models의 실제 모델 정의 사용
    probe_engine.dispose()

    conn = sqlite3.connect(str(db_path))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agentreport)")}
    conn.close()
    assert "provider_supports_tools" not in columns


def test_init_db_adds_missing_column_to_old_schema_db(tmp_path, monkeypatch):
    """database.init_db()를 구버전 DB 파일에 대해 실행하면 provider_supports_tools
    컬럼이 NOT NULL DEFAULT 0으로 추가되어야 한다. 마이그레이션 SQL을 지우거나
    컬럼명을 틀리면 이 테스트가 깨진다.
    """
    db_path = tmp_path / "old_schema.db"
    _create_old_schema_agentreport(str(db_path))

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    conn = sqlite3.connect(str(db_path))
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(agentreport)")}
    conn.close()
    assert "provider_supports_tools" in cols
    _, name, col_type, notnull, default, _pk = cols["provider_supports_tools"]
    assert col_type.upper() in ("BOOLEAN", "BOOL")
    assert notnull == 1


def test_pre_existing_row_does_not_read_as_provider_supports_tools(tmp_path, monkeypatch):
    """마이그레이션이 채우는 기본값은 반드시 False(0)여야 한다. 구버전 DB에 이미
    저장돼 있던, 도구 없이 지어낸 리포트가 마이그레이션 한 번으로 "도구 지원"으로
    둔갑하면 #162가 지적한 결함보다 더 나쁜 상태가 된다. 기본값을 1이나 NULL로
    바꾸면 이 테스트가 깨진다.
    """
    db_path = tmp_path / "old_schema_row.db"
    _create_old_schema_agentreport(str(db_path))

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    from sqlmodel import Session, select

    from backend.models import AgentReport

    with Session(test_engine) as session:
        rows = session.exec(select(AgentReport)).all()
        assert len(rows) == 1
        assert rows[0].stock_name == "삼성전자"
        assert rows[0].provider_supports_tools is False

    # ORM 계층을 우회해 raw SQLite 값도 직접 확인 (0이어야 하며 NULL이 아니어야 함)
    conn = sqlite3.connect(str(db_path))
    raw_value = conn.execute("SELECT provider_supports_tools FROM agentreport").fetchone()[0]
    conn.close()
    assert raw_value == 0


def test_init_db_is_idempotent_on_already_migrated_db(tmp_path, monkeypatch):
    """init_db()를 두 번 호출해도(재기동 시나리오) 이미 컬럼이 있으면 ALTER TABLE을
    다시 시도해 예외를 던지지 않아야 한다.
    """
    db_path = tmp_path / "already_migrated.db"
    _create_old_schema_agentreport(str(db_path))

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()
    database.init_db()  # 두 번째 호출에서 예외가 나면 테스트 실패

    conn = sqlite3.connect(str(db_path))
    columns = [row[1] for row in conn.execute("PRAGMA table_info(agentreport)")]
    conn.close()
    assert columns.count("provider_supports_tools") == 1


def test_init_db_concurrent_migration_does_not_abort_startup(tmp_path, monkeypatch):
    """MEDIUM3: check(컬럼 존재 확인)와 act(ALTER TABLE)가 원자적이지 않으므로,
    여러 워커가 동시에 init_db()를 호출하면(현재는 Dockerfile이 --workers 없이
    단일 프로세스라 해당 없지만, 그 가정이 바뀌면 바로 재현된다) 컬럼을 먼저 추가한
    쪽을 제외한 나머지가 'duplicate column name' OperationalError로 lifespan을
    타고 올라가 startup 자체를 abort시킬 수 있다. 재현: 구버전 DB 하나에 스레드
    여러 개가 동시에 init_db()를 호출한다 — 수정 전에는 재현 환경에서 6개 중
    5개가 예외를 던졌다.
    """
    db_path = tmp_path / "concurrent_migration.db"
    _create_old_schema_agentreport(str(db_path))

    # timeout(=busy_timeout)을 넉넉히 줘서 "database is locked"으로 인한 재현
    # 불안정성을 줄인다 - 우리가 잡으려는 것은 락 경합이 아니라 duplicate column race다.
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    monkeypatch.setattr(database, "engine", test_engine)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    thread_count = 6
    barrier = threading.Barrier(thread_count)

    def worker() -> None:
        barrier.wait()
        try:
            database.init_db()
        except BaseException as exc:  # noqa: BLE001 - 테스트가 모든 실패를 관찰해야 함
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"init_db()가 동시 호출에서 예외를 던졌다: {errors!r}"

    conn = sqlite3.connect(str(db_path))
    columns = [row[1] for row in conn.execute("PRAGMA table_info(agentreport)")]
    row_count = conn.execute("SELECT COUNT(*) FROM agentreport").fetchone()[0]
    conn.close()
    assert columns.count("provider_supports_tools") == 1
    assert row_count == 1  # 기존 행이 중복 삽입되지 않았는지도 함께 확인


def test_init_db_concurrent_on_empty_db_does_not_abort_startup(tmp_path, monkeypatch):
    """🟡2: 빈 DB(사전 스키마 없음)에서 여러 워커가 동시에 init_db()를 호출해도
    startup이 abort되지 않아야 한다.

    test_init_db_concurrent_migration_does_not_abort_startup는 구버전 스키마가
    이미 존재하는 경우(ALTER TABLE 경합)를 검증한다. 이 테스트는 그것과 달리
    _create_old_schema_agentreport()로 사전 스키마를 만들지 않고, 완전히 빈 DB에서
    create_all() 자체가 "table already exists" 경합을 일으키는 경로를 고정한다.
    수정 전에는 init_db()의 create_all()이 OperationalError를 흡수하지 않아
    늦게 도착한 워커가 startup을 abort시킬 수 있었다.
    """
    db_path = tmp_path / "empty_concurrent.db"
    # 빈 DB: 사전 스키마 생성 없이 바로 동시 호출
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    monkeypatch.setattr(database, "engine", test_engine)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    thread_count = 6
    barrier = threading.Barrier(thread_count)

    def worker() -> None:
        barrier.wait()
        try:
            database.init_db()
        except BaseException as exc:  # noqa: BLE001 - 테스트가 모든 실패를 관찰해야 함
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"빈 DB에서 동시 init_db()가 예외를 던졌다: {errors!r}"

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    columns = [row[1] for row in conn.execute("PRAGMA table_info(agentreport)")]
    conn.close()
    assert "provider_supports_tools" in columns


# ── A (#162): decision/confidence_score nullable 마이그레이션 ────────────────


def _create_post_c_schema_agentreport(db_path: str) -> None:
    """PR #171(C 구현) 이후, 이 PR(A 구현) 이전의 agentreport 스키마를 재현한다.

    _create_old_schema_agentreport는 Column(String) 기본값(nullable=True)을 쓰므로
    decision/confidence_score가 이미 nullable이다. 반면 실제 배포된 DB는 SQLModel
    `decision: str = Field(...)` 선언에서 NOT NULL로 만들어졌다. A 마이그레이션
    (_run_table_recreate_migrations)이 실제로 NOT NULL → nullable 변환을 수행하는지
    검증하려면 NOT NULL 컬럼을 가진 테이블이 필요하다.
    """
    import sqlite3
    from sqlmodel import SQLModel

    setup_engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(setup_engine)  # 최신 스키마로 다른 테이블들 생성
    setup_engine.dispose()

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE agentreport")
    conn.execute(
        "CREATE TABLE agentreport ("
        "id INTEGER PRIMARY KEY, "
        "stock_code VARCHAR NOT NULL, "
        "stock_name VARCHAR NOT NULL, "
        "provider VARCHAR NOT NULL, "
        "summary VARCHAR NOT NULL, "
        "decision VARCHAR NOT NULL, "           # C 이전처럼 NOT NULL
        "confidence_score FLOAT NOT NULL, "     # C 이전처럼 NOT NULL
        "reason VARCHAR NOT NULL DEFAULT '', "
        "provider_supports_tools BOOLEAN NOT NULL DEFAULT 0, "
        "created_at DATETIME NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO agentreport "
        "(stock_code, stock_name, provider, summary, decision, confidence_score, "
        "reason, provider_supports_tools, created_at) "
        "VALUES ('005930', '삼성전자', 'openai', '요약', 'BUY', 0.9, '지어낸 근거', 0, "
        "'2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()


def test_init_db_makes_decision_and_confidence_score_nullable(tmp_path, monkeypatch):
    """NOT NULL decision/confidence_score를 가진 구버전 DB에 init_db()를 실행하면
    두 컬럼이 nullable로 변경되어야 한다. _run_table_recreate_migrations()가 호출되지
    않거나 DDL에서 nullable을 제거하면 이 테스트가 깨진다.
    """
    import sqlite3

    db_path = tmp_path / "post_c_schema_not_null.db"
    _create_post_c_schema_agentreport(str(db_path))  # decision NOT NULL인 상태

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    conn = sqlite3.connect(str(db_path))
    col_info = {row[1]: row for row in conn.execute("PRAGMA table_info(agentreport)")}
    conn.close()

    # notnull 비트(인덱스 3)가 0이어야 nullable
    assert col_info["decision"][3] == 0, "decision이 여전히 NOT NULL이다"
    assert col_info["confidence_score"][3] == 0, "confidence_score가 여전히 NOT NULL이다"


def test_nullable_migration_preserves_existing_rows(tmp_path, monkeypatch):
    """테이블 재생성 마이그레이션이 기존 데이터를 그대로 보존해야 한다.
    INSERT SELECT가 빠지거나 컬럼 목록이 어긋나면 이 테스트가 깨진다.
    """
    import sqlite3

    db_path = tmp_path / "preserve_rows.db"
    _create_post_c_schema_agentreport(str(db_path))  # decision="BUY", confidence_score=0.9로 행 삽입됨

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT stock_name, decision, confidence_score, provider_supports_tools FROM agentreport"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    stock_name, decision, confidence_score, pst = rows[0]
    assert stock_name == "삼성전자"
    assert decision == "BUY"                     # 기존 NOT NULL 값은 보존
    assert abs(confidence_score - 0.9) < 1e-9    # 기존 값 보존
    assert pst == 0                               # provider_supports_tools 기본값 유지


def test_nullable_migration_allows_null_decision_insert(tmp_path, monkeypatch):
    """마이그레이션 후 decision/confidence_score에 NULL을 INSERT할 수 있어야 한다.
    도구 없는 provider 리포트가 실제로 저장될 수 있는지 확인한다.
    마이그레이션이 NOT NULL을 남겨두면 이 INSERT가 실패해 테스트가 깨진다.
    """
    import sqlite3

    db_path = tmp_path / "null_insert.db"
    _create_post_c_schema_agentreport(str(db_path))  # decision NOT NULL인 상태

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO agentreport "
        "(stock_code, stock_name, provider, summary, decision, confidence_score, "
        "reason, provider_supports_tools, created_at) "
        "VALUES ('005930', '삼성전자', 'openai', '배경 설명', NULL, NULL, "
        "'', 0, '2026-01-02 00:00:00')"
    )
    conn.commit()
    # created_at으로 새로 삽입한 행(도구 없는 provider)만 필터링한다.
    # 기존 행('2026-01-01')과 구분하기 위해 날짜를 다르게 지정했다.
    rows = conn.execute(
        "SELECT decision, confidence_score FROM agentreport WHERE created_at='2026-01-02 00:00:00'"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0][0] is None   # decision=NULL
    assert rows[0][1] is None   # confidence_score=NULL


def test_nullable_migration_is_idempotent(tmp_path, monkeypatch):
    """이미 nullable인 DB에 init_db()를 두 번 호출해도 예외 없이 통과해야 한다."""
    import sqlite3

    db_path = tmp_path / "idempotent_recreate.db"
    _create_post_c_schema_agentreport(str(db_path))  # NOT NULL 상태에서 시작

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()
    database.init_db()  # 두 번째 호출에서 예외가 나면 테스트 실패

    conn = sqlite3.connect(str(db_path))
    columns = [row[1] for row in conn.execute("PRAGMA table_info(agentreport)")]
    conn.close()
    assert "decision" in columns
    assert "confidence_score" in columns
