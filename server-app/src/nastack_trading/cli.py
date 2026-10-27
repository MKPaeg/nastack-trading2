import argparse

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .catalog import import_note
from .config import ROOT, Config
from .db import connect
from .models import User
from .security import bootstrap, issue_admin_code


def migrate(url):
    config = AlembicConfig(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["migrate", "bootstrap", "issue-admin-code", "register-admin", "import-sources"]
    )
    parser.add_argument("--email", help="접근 코드를 발급할 관리자 이메일")
    parser.add_argument("--reissue", action="store_true", help="기존 관리자 코드 교체 및 해당 계정 세션 해지")
    args = parser.parse_args()
    if (args.email or args.reissue) and args.command not in {
        "issue-admin-code",
        "register-admin",
        "import-sources",
    }:
        parser.error("--email/--reissue는 issue-admin-code에서만 사용할 수 있습니다.")
    if args.command in {"issue-admin-code", "register-admin"} and not args.email:
        parser.error("--email로 관리자 이메일을 명시하세요.")
    config = Config(admin_email=args.email) if args.email else Config()
    if args.command == "migrate":
        migrate(config.database_url)
    else:
        engine, factory = connect(config.database_url)
        try:
            if args.command == "import-sources":
                if not args.email or args.reissue:
                    parser.error("import-sources에는 기존 관리자 --email만 지정하세요.")
                with factory.begin() as db:
                    admin = db.scalar(
                        select(User).where(
                            User.email == str(config.admin_email).lower(),
                            User.role == "admin",
                            User.active.is_(True),
                        )
                    )
                    if not admin:
                        parser.error("등록된 활성 관리자 이메일을 지정하세요.")
                    notes = list((ROOT / "docs").glob("*ETF*API*.md"))
                    if len(notes) != 1:
                        parser.error("기준 소스 노트를 한 개 지정할 수 있어야 합니다.")
                    count = import_note(db, admin, notes[0])
                print(f"소스 후보 {count}개 등록 (기존 항목 변경 없음)")
                return
            if args.command == "register-admin":
                if args.reissue:
                    parser.error("이메일 인증 방식에서는 코드 재발급 대신 화면에서 메일을 요청하세요.")
                issue_admin_code(factory, config)
                print("관리자 이메일을 등록했습니다. 로그인 화면에서 인증 메일을 요청하세요.")
                return
            if args.command == "issue-admin-code":
                code = issue_admin_code(factory, config, reissue=args.reissue)
                print(f"관리자 이메일: {config.admin_email}")
                print(f"접근 코드 (이번에만 표시): {code}")
                print("안전한 곳에 보관한 후 로그인하세요. 이메일로 발송하지 않았습니다.")
                return
            bootstrap(factory, config)
        except ValueError as error:
            parser.error(str(error))
        except SQLAlchemyError:
            parser.error("DB 연결 또는 migration을 확인하세요. 접근 코드 발급을 완료하지 못했습니다.")
        finally:
            engine.dispose()
    print(f"{args.command}: OK")


if __name__ == "__main__":
    main()
