import pytest
from rich.console import Console

from dbsync.config import mask_dsn
from dbsync.database import ColumnSchema, ForeignKeySchema, SchemaSnapshot, TableSchema
from dbsync.decisions import AutoDecider, InteractiveDecider, SyncAbortedError
from dbsync.runner import SchemaDiff, _dependency_order, print_plan


def table(name: str, references: tuple[str, ...] = ()) -> TableSchema:
    fks = tuple(
        ForeignKeySchema(
            constraint_name=f"{name}_{parent}_fk",
            columns=(f"{parent}_id",),
            referenced_table=parent,
            referenced_columns=("id",),
            on_update="NO ACTION",
            on_delete="NO ACTION",
            match_option=None,
        )
        for parent in references
    )
    columns = (
        ColumnSchema(name="id", column_type="integer", is_nullable=False, default=None),
    )
    return TableSchema(
        name=name, columns=columns, primary_key=("id",), foreign_keys=fks
    )


def test_auto_decider_follows_policy():
    strict = AutoDecider()
    permissive = AutoDecider(allow_drop=True, not_null_default="n/a")

    assert strict.confirm_drop(["users.legacy"]) is False
    assert permissive.confirm_drop(["users.legacy"]) is True
    assert permissive.resolve_not_null("users", "name") == ("default", "n/a")
    with pytest.raises(SyncAbortedError, match="users.name"):
        strict.resolve_not_null("users", "name")


def test_auto_decider_prefers_primary_key():
    decider = AutoDecider()

    assert decider.choose_sync_key("users", ["email", "id"], ("id",)) == ("id",)
    assert decider.choose_sync_key("users", ["email"], ("id",)) == ("email",)
    assert decider.choose_sync_key("users", [], ("id", "tenant")) == ("id", "tenant")
    assert decider.choose_sync_key("logs", [], ()) is None


def test_interactive_decider_asks_until_valid(monkeypatch: pytest.MonkeyPatch):
    answers = iter(["x", "3", "2", "v", "unknown"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    decider = InteractiveDecider()

    assert decider.choose_sync_key("users", ["email", "id"], ("id",)) == ("id",)
    assert decider.resolve_not_null("users", "name") == ("default", "unknown")


def test_parents_are_synced_before_children():
    schema = SchemaSnapshot.from_tables(
        [
            table("order_items", ("orders", "products")),
            table("orders", ("users",)),
            table("products"),
            table("users"),
        ]
    )

    order = _dependency_order(schema)

    assert order.index("users") < order.index("orders") < order.index("order_items")
    assert order.index("products") < order.index("order_items")
    assert sorted(order) == sorted(schema.tables)


def test_dependency_cycle_does_not_hang():
    schema = SchemaSnapshot.from_tables(
        [table("a", ("b",)), table("b", ("a",)), table("tree", ("tree",))]
    )

    assert sorted(_dependency_order(schema)) == ["a", "b", "tree"]


@pytest.mark.parametrize(
    ("dsn", "masked"),
    [
        ("postgresql://user:s3cret@db:5432/prod", "postgresql://user:***@db:5432/prod"),
        ("postgresql://user@db/prod", "postgresql://user@db/prod"),
        ("dbname=prod user=postgres", "dbname=prod user=postgres"),
    ],
)
def test_mask_dsn(dsn: str, masked: str):
    assert mask_dsn(dsn) == masked


def test_print_plan():
    console = Console(record=True, width=120)
    diff = SchemaDiff(
        new_tables=[table("orders", ("users",))],
        missing_tables=[table("legacy")],
        orphan_columns={"users": ["old_flag"]},
        missing_columns={"users": [ColumnSchema("name", "text", True, None)]},
        missing_foreign_keys={"orders": list(table("orders", ("users",)).foreign_keys)},
    )

    print_plan(diff, console)

    text = console.export_text()
    assert "+ CREATE TABLE orders" in text
    assert "+ ADD COLUMN users.name text" in text
    assert "+ ADD FOREIGN KEY orders(users_id) → users(id)" in text
    assert "- DROP COLUMN? users.old_flag" in text
    assert "- DROP TABLE? legacy" in text
