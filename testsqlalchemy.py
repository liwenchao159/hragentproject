from traceback import print_tb
from numpy import full
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Select,
    String,
    Table,
    text,
    create_engine,
    ForeignKey,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    Mapped,
    mapped_column,
    relationship,
    Session,
)
from typing import Optional


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(30))
    fullname: Mapped[Optional[str]] = mapped_column(String(100))
    addresses: Mapped[Optional[list["Address"]]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, name={self.name!r})"


class Address(Base):
    __tablename__ = "user_address"
    id: Mapped[int] = mapped_column(primary_key=True)
    email_address: Mapped[str] = mapped_column(String(50))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    user: Mapped["User"] = relationship(back_populates="addresses")

    def __repr__(self) -> str:
        return f"Address(id={self.id!r}, email_address={self.email_address!r})"


metaobj = MetaData()


engine = create_engine("sqlite+pysqlite:///test.db", echo=True)
Base.metadata.create_all(engine)
metaobj.create_all(engine)
test_table = Table(
    "test_table",
    metaobj,
    autoload_with=engine,
)
print(test_table.c.name)
print(test_table.c.keys())
with Session(engine) as session:
    # newuser = User(name="lwchao", fullname="李文超")
    # addr = Address(email_address="test@xxx.com", user=newuser)
    # session.add(newuser)
    # session.commit()
    # user = session.query(User).filter_by(name="lwchao").first()
    # print(user.addresses[0].email_address)

    stmt = Select(User).where(User.name == "lwchao")
    for user in session.scalars(stmt):
        print(user)
        for addr in user.addresses:
            print(addr.email_address)
