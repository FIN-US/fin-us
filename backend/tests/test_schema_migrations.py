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


def _agentreport_index_names(db_path: str) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='agentreport'"
            )
            if row[0] is not None
        )
    finally:
        conn.close()


def test_leftover_agentreport_new_does_not_prevent_migration(tmp_path, monkeypatch):
    """Critical (#162 리뷰): agentreport_new가 이미 존재하는 DB에서도 init_db()가 성공해야 한다.

    pysqlite는 DML에서만 암묵적으로 BEGIN하므로 CREATE TABLE agentreport_new는
    engine.begin() 블록 진입 시점에도 autocommit으로 즉시 디스크에 커밋된다.
    SIGTERM/OOM kill/디스크 부족 등으로 마이그레이션이 중단되면 agentreport_new가
    남고, 다음 부팅에서 CREATE TABLE "already exists" → 재확인에서 decision이 여전히
    NOT NULL → raise → 영구 부팅 불능이 된다.

    이 테스트가 잡는 mutation: DROP TABLE IF EXISTS agentreport_new 줄 제거.
    """
    db_path = tmp_path / "leftover_new.db"
    _create_post_c_schema_agentreport(str(db_path))  # agentreport: decision NOT NULL

    # 이전 시도가 중단된 상황을 재현: agentreport_new 고아 테이블을 미리 만들어 둔다.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE agentreport_new ("
        "id INTEGER PRIMARY KEY, "
        "stock_code VARCHAR NOT NULL, "
        "stock_name VARCHAR NOT NULL, "
        "provider VARCHAR NOT NULL, "
        "summary VARCHAR NOT NULL, "
        "decision VARCHAR, "
        "confidence_score FLOAT, "
        "reason VARCHAR NOT NULL DEFAULT '', "
        "provider_supports_tools BOOLEAN NOT NULL DEFAULT 0, "
        "created_at DATETIME NOT NULL"
        ")"
    )
    conn.commit()
    conn.close()

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    # 고아 테이블이 있어도 init_db()가 예외 없이 완료되어야 한다.
    database.init_db()

    conn = sqlite3.connect(str(db_path))
    col_info = {row[1]: row for row in conn.execute("PRAGMA table_info(agentreport)")}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()

    assert col_info["decision"][3] == 0, "decision이 여전히 NOT NULL이다"
    assert "agentreport_new" not in tables, "agentreport_new 고아 테이블이 남아 있다"


def test_column_set_mismatch_raises_before_recreation(tmp_path, monkeypatch):
    """Improvement #1 (#162 리뷰): 실제 컬럼 집합이 DDL 가정과 다를 때 RuntimeError를 던져야 한다.

    _PENDING_COLUMN_MIGRATIONS에 agentreport의 새 컬럼이 추가된 뒤 이 함수의 DDL을
    갱신하지 않으면 컬럼과 데이터가 조용히 소실된다. 단언으로 명시적 부팅 실패를 만든다.

    이 테스트가 잡는 mutation: _RECREATE_AGENTREPORT_COL_SET 불일치 단언 제거.
    """
    db_path = tmp_path / "extra_col.db"
    _create_post_c_schema_agentreport(str(db_path))  # agentreport: decision NOT NULL

    # DDL이 모르는 컬럼 extra_col을 추가해 불일치 상황을 재현한다.
    conn = sqlite3.connect(str(db_path))
    conn.execute("ALTER TABLE agentreport ADD COLUMN extra_col VARCHAR")
    conn.commit()
    conn.close()

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    import pytest
    with pytest.raises(RuntimeError, match="agentreport 스키마가 재생성 DDL의 가정과 다릅니다"):
        database.init_db()


def test_nullable_migration_preserves_indexes(tmp_path, monkeypatch):
    """테이블 재생성 마이그레이션이 stock_code·stock_name 인덱스를 보존해야 한다.

    DROP TABLE은 그 테이블에 딸린 인덱스도 함께 지운다. 재생성 후 CREATE INDEX를
    다시 실행하지 않으면 models.py의 index=True로 선언된 두 인덱스가 영구히 사라진다 —
    테이블이 이미 존재하므로 create_all()이 다시 만들어 주지 않기 때문에 조용한 성능
    저하로만 남는다.

    이 테스트가 잡는 mutation: _RECREATE_AGENTREPORT_INDEX_DDL 실행 루프 제거.
    """
    db_path = tmp_path / "preserve_indexes.db"
    _create_post_c_schema_agentreport(str(db_path))

    # 픽스처는 create_all이 만든 테이블을 DROP하고 인덱스 없이 다시 만든다.
    # 실제 배포 DB에는 SQLModel이 붙인 인덱스가 있으므로 그 상태를 재현한다.
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE INDEX ix_agentreport_stock_code ON agentreport (stock_code)")
    conn.execute("CREATE INDEX ix_agentreport_stock_name ON agentreport (stock_name)")
    conn.commit()
    conn.close()

    before = _agentreport_index_names(str(db_path))
    assert before == ["ix_agentreport_stock_code", "ix_agentreport_stock_name"], (
        f"이 테스트의 전제(마이그레이션 전 인덱스 2개 존재)가 깨졌습니다: {before}"
    )

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    assert _agentreport_index_names(str(db_path)) == before, (
        "테이블 재생성 마이그레이션이 인덱스를 잃었습니다"
    )


# --- #298: 신호 점수화 컬럼 -------------------------------------------------


def test_init_db_adds_nullable_signal_scoring_columns(tmp_path, monkeypatch):
    """구버전 DB에 signal_score·signal_reason·signal_uncertainty가 추가되어야 한다.

    셋 다 NULL 허용이어야 하고 DEFAULT가 없어야 한다. provider_supports_tools처럼
    DEFAULT 0을 주면 "모델이 0점(무관/중립)으로 채점했다"는 없던 사실이 과거 행에
    소급 생성된다 — #122·#162의 "0과 모름 구분" 원칙 위반이다.

    이 테스트가 잡는 mutation: _PENDING_COLUMN_MIGRATIONS의 세 항목 제거,
    또는 NOT NULL DEFAULT 0 추가.
    """
    db_path = tmp_path / "old_schema_signal.db"
    _create_old_schema_agentreport(str(db_path))

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    conn = sqlite3.connect(str(db_path))
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(agentreport)")}
    raw = conn.execute(
        "SELECT signal_score, signal_reason, signal_uncertainty FROM agentreport"
    ).fetchone()
    conn.close()

    expected_types = {
        "signal_score": "INTEGER",
        "signal_reason": "VARCHAR",
        "signal_uncertainty": "FLOAT",
    }
    for name, expected_type in expected_types.items():
        assert name in cols, f"{name} 컬럼이 추가되지 않았다"
        _, _, col_type, notnull, default, _pk = cols[name]
        assert col_type.upper() == expected_type
        assert notnull == 0, f"{name}이 NOT NULL이면 '모름'을 표현할 수 없다"
        assert default is None, f"{name}에 DEFAULT가 있으면 과거 행에 없던 점수가 생긴다"

    # 마이그레이션 이전에 저장된 행은 세 값 모두 NULL로 남아야 한다.
    assert raw == (None, None, None)


def _create_pre_signal_scoring_agentreport(db_path: str) -> None:
    """#298 이전이면서 decision이 아직 NOT NULL인 agentreport를 재현한다.

    signal_* 컬럼은 _run_schema_migrations()의 ALTER로 먼저 붙으므로, 테이블 재생성
    (_run_table_recreate_migrations)이 도는 시점에는 이미 존재한다. 재생성 DDL이
    이 컬럼들을 모르면 컬럼과 데이터가 통째로 사라진다 — 그 경로를 고정한다.
    """
    _create_post_c_schema_agentreport(db_path)

    conn = sqlite3.connect(db_path)
    for alter in (
        "ALTER TABLE agentreport ADD COLUMN signal_score INTEGER",
        "ALTER TABLE agentreport ADD COLUMN signal_reason VARCHAR",
        "ALTER TABLE agentreport ADD COLUMN signal_uncertainty FLOAT",
    ):
        conn.execute(alter)
    conn.execute(
        "UPDATE agentreport SET signal_score = -2, signal_reason = '수주 취소 공시', "
        "signal_uncertainty = 1.25"
    )
    conn.commit()
    conn.close()


def test_nullable_recreation_preserves_signal_scoring_columns(tmp_path, monkeypatch):
    """테이블 재생성 마이그레이션이 signal_* 컬럼과 그 값을 보존해야 한다.

    이 테스트가 잡는 mutation: _RECREATE_AGENTREPORT_NULLABLE_DDL 또는
    _RECREATE_AGENTREPORT_COLS에서 signal_* 누락 (누락 시 RuntimeError로 부팅이
    막히거나, DDL만 고치고 COLS를 빠뜨리면 값이 조용히 사라진다).
    """
    db_path = tmp_path / "pre_signal_scoring.db"
    _create_pre_signal_scoring_agentreport(str(db_path))

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    conn = sqlite3.connect(str(db_path))
    col_info = {row[1]: row for row in conn.execute("PRAGMA table_info(agentreport)")}
    row = conn.execute(
        "SELECT signal_score, signal_reason, signal_uncertainty FROM agentreport"
    ).fetchall()
    conn.close()

    assert col_info["decision"][3] == 0, "재생성이 수행되지 않았다 (decision이 여전히 NOT NULL)"
    assert row == [(-2, "수주 취소 공시", 1.25)], "재생성이 신호 점수 값을 잃었다"


def test_init_db_creates_filtered_signal_table_on_existing_db(tmp_path, monkeypatch):
    """#304: 이미 쓰고 있던 DB 파일에도 filteredsignal 테이블이 생겨야 한다.

    새 테이블은 create_all()이 만들어 주므로 _PENDING_COLUMN_MIGRATIONS에 넣을
    필요가 없다 — 하지만 그 전제가 깨지면 기록 경로가 조용히 실패하고(로그만 남고)
    임계값 조정 근거는 계속 없는 상태가 된다. 전제를 여기서 못박는다.
    """
    db_path = tmp_path / "existing.db"
    _create_old_schema_agentreport(str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE IF EXISTS filteredsignal")
    conn.commit()
    conn.close()

    test_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    conn = sqlite3.connect(str(db_path))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(filteredsignal)")}
    conn.close()
    assert columns == {
        "id",
        "stock_name",
        "source",
        "score",
        "threshold",
        "reason",
        "uncertainty",
        "created_at",
    }


# ---------------------------------------------------------------------------
# 체결 통지 outbox 컬럼 (#259 2단계)
# ---------------------------------------------------------------------------


def _create_old_schema_tradehistory(db_path: str) -> None:
    """notified_at 컬럼이 없던 시절의 tradehistory를 재현하고 행 하나를 남긴다.

    다른 테이블은 실제 배포처럼 최신 스키마로 둔다 — 이 변경이 건드린 것은 이 테이블
    하나뿐이라 나머지가 구버전일 이유가 없다(위 agentreport 헬퍼와 같은 이유).
    """
    from sqlmodel import SQLModel

    setup_engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(setup_engine)

    old_metadata = MetaData()
    Table(
        "tradehistory",
        old_metadata,
        Column("id", Integer, primary_key=True),
        Column("stock_code", String),
        Column("stock_name", String),
        Column("trade_type", String),
        Column("quantity", Integer),
        Column("price", Float),
        Column("trade_date", DateTime),
    )
    with setup_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE tradehistory")
    old_metadata.create_all(setup_engine)
    with setup_engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO tradehistory "
            "(stock_code, stock_name, trade_type, quantity, price, trade_date) "
            "VALUES ('005930', '삼성전자', 'BUY', 1, 75000, '2026-01-01 00:00:00')"
        )
    setup_engine.dispose()


def test_init_db_adds_notified_at_to_old_trade_history(tmp_path, monkeypatch):
    """구버전 tradehistory에도 outbox 상태 컬럼이 생긴다.

    이 컬럼이 없으면 재배달 조회 자체가 터져 체결 통지 outbox가 통째로 죽는다.
    """
    db_path = tmp_path / "old_trade_history.db"
    _create_old_schema_tradehistory(str(db_path))

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    conn = sqlite3.connect(str(db_path))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tradehistory)")}
    conn.close()
    assert "notified_at" in columns


def test_migration_does_not_resurrect_notifications_for_old_trades(tmp_path, monkeypatch):
    """컬럼 추가와 함께 기존 행이 통지 완료로 백필된다 (#259 2단계).

    백필이 없으면 배포 직후 첫 주기가 창(24시간) 안의 과거 체결을 전부 "통지가
    늦었습니다"로 다시 알린다. outbox가 이 행들의 통지를 책임진 적이 없으므로 그것은
    복구가 아니라 없던 통지를 새로 만드는 것이다. 값은 지어내지 않고 trade_date를 쓴다.
    """
    db_path = tmp_path / "backfilled_trade_history.db"
    _create_old_schema_tradehistory(str(db_path))

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT trade_date, notified_at FROM tradehistory").fetchall()
    conn.close()
    assert len(rows) == 1
    trade_date, notified_at = rows[0]
    assert notified_at is not None
    assert notified_at == trade_date


def test_backfill_does_not_run_again_on_an_already_migrated_db(tmp_path, monkeypatch):
    """백필은 컬럼이 방금 없었을 때만 돈다.

    부팅마다 무조건 도는 자리에 두면 아직 통지되지 않은 행(notified_at IS NULL)까지
    통지된 것으로 덮어, 체결됐는데 아무 말도 못 받는 상태가 조용히 확정된다.
    """
    db_path = tmp_path / "already_backfilled.db"
    _create_old_schema_tradehistory(str(db_path))

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    # 마이그레이션 이후에 들어온 미통지 체결. 재부팅이 이걸 덮으면 안 된다.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tradehistory "
        "(stock_code, stock_name, trade_type, quantity, price, trade_date, notified_at) "
        "VALUES ('035420', 'NAVER', 'BUY', 2, 200000, '2026-02-01 00:00:00', NULL)"
    )
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(str(db_path))
    pending = conn.execute(
        "SELECT stock_name FROM tradehistory WHERE notified_at IS NULL"
    ).fetchall()
    conn.close()
    assert [row[0] for row in pending] == ["NAVER"]


# --- #196: 시세 신선도 컬럼 -------------------------------------------------


def _create_old_schema_portfolio(db_path: str) -> None:
    """price_updated_at 컬럼이 없던 시절의 portfolio 테이블을 재현한다.

    current_price에 값이 들어 있는 행을 한 건 넣는다 — 이 이슈(#196)의 핵심은
    "값은 있는데 언제 채운 값인지 모르는 행"이며, 백필 여부는 그 행에 대해서만
    관측 가능하기 때문이다.

    다른 테이블은 _create_old_schema_agentreport와 같은 이유로 최신 스키마 그대로 둔다.
    """
    from sqlmodel import SQLModel

    setup_engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(setup_engine)

    old_metadata = MetaData()
    Table(
        "portfolio",
        old_metadata,
        Column("id", Integer, primary_key=True),
        Column("stock_code", String),
        Column("stock_name", String),
        Column("quantity", Integer),
        Column("avg_price", Float),
        Column("current_price", Float),
        Column("updated_at", DateTime),
    )
    with setup_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE portfolio")
    old_metadata.create_all(setup_engine)
    with setup_engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO portfolio "
            "(stock_code, stock_name, quantity, avg_price, current_price, updated_at) "
            "VALUES ('005930', '삼성전자', 10, 70000, 77000, '2026-01-01 00:00:00')"
        )
    setup_engine.dispose()


def test_init_db_adds_nullable_price_updated_at_column(tmp_path, monkeypatch):
    """구버전 DB에 portfolio.price_updated_at이 NULL 허용·DEFAULT 없이 추가되어야 한다.

    백필은 **하지 않는다**. 이 컬럼이 없던 시절의 행은 current_price가 채워져 있어도
    그 값이 언제 채워졌는지 알 수 없다. updated_at으로 백필하면 "잔고를 마지막으로
    확인한 시각"이 "시세를 마지막으로 갱신한 시각"으로 둔갑해, 낡은 시세가 방금 갱신된
    것처럼 보인다 — #196이 없애려는 "모르는데 안다"가 정확히 그 형태다.

    이 테스트가 잡는 mutation: _PENDING_COLUMN_MIGRATIONS의 portfolio 항목 제거,
    NOT NULL/DEFAULT 추가, 또는 "UPDATE portfolio SET price_updated_at = updated_at"
    같은 백필 문장 추가.
    """
    db_path = tmp_path / "old_schema_portfolio.db"
    _create_old_schema_portfolio(str(db_path))

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", test_engine)

    database.init_db()

    conn = sqlite3.connect(str(db_path))
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(portfolio)")}
    raw = conn.execute(
        "SELECT current_price, price_updated_at FROM portfolio"
    ).fetchall()
    conn.close()

    assert "price_updated_at" in cols, "price_updated_at 컬럼이 추가되지 않았다"
    _, _, col_type, notnull, default, _pk = cols["price_updated_at"]
    assert col_type.upper() == "DATETIME"
    assert notnull == 0, "NOT NULL이면 '시세 나이 모름'을 표현할 수 없다"
    assert default is None, "DEFAULT가 있으면 과거 행에 없던 신선도가 생긴다"

    # 값이 있는 구버전 행도 나이는 NULL로 남아야 한다 (백필 금지).
    assert raw == [(77000.0, None)], (
        "구버전 행의 price_updated_at을 백필하면 낡은 시세가 신선한 것으로 단언된다"
    )
