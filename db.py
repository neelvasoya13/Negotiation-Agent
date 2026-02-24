import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
load_dotenv()

def get_db_connection():
    """
    Create and return a PostgreSQL connection.

    Uses DATABASE_URL env var if present, otherwise falls back to a local default.
    Example DATABASE_URL:
    postgresql://user:password@localhost:5432/construction_negotiation
    """
    database_url = os.getenv("DATABASE_URL")
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)


@dataclass
class MaterialInfo:
    material_id: int
    material_name: str
    brand: Optional[str]
    unit: str
    base_cost: float
    stock_quantity: int

@dataclass
class BuilderInfo:
    builder_id: int
    builder_name: str
    city: Optional[str]
    payment_history: str
    total_orders: int
    total_value: float


def fetch_material_by_name_and_brand(
    material_name: str, brand: Optional[str] = None
) -> Optional[MaterialInfo]:
    """
    Fetch a single material row from materials table.

    SQL:
    SELECT material_id, material_name, brand, unit,
           base_cost, stock_quantity
    FROM materials
    WHERE LOWER(material_name) = LOWER(%s)
      AND (%s IS NULL OR LOWER(brand) = LOWER(%s))
    ORDER BY last_updated DESC
    LIMIT 1;
    """
    query = """
        SELECT material_id, material_name, brand, unit,
               base_cost, stock_quantity
        FROM materials
        WHERE LOWER(material_name) = LOWER(%s)
          AND (%s IS NULL OR LOWER(brand) = LOWER(%s))
        ORDER BY last_updated DESC
        LIMIT 1;
    """
    params: Tuple[Any, ...] = (material_name, brand, brand)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()

    if not row:
        return None

    return MaterialInfo(
        material_id=row["material_id"],
        material_name=row["material_name"],
        brand=row.get("brand"),
        unit=row["unit"],
        base_cost=float(row["base_cost"]),
        stock_quantity=row["stock_quantity"],
    )


def fetch_builder_by_email_and_password(
    email: str, password: str
) -> Optional[BuilderInfo]:
    """
    Fetch builder by email and verify password for login.
    Returns builder info if credentials match, else None.
    """
    query = """
        SELECT builder_id, builder_name, city, payment_history,
               total_orders, total_value
        FROM builders
        WHERE LOWER(email) = LOWER(%s) AND password = %s
        ORDER BY created_at DESC
        LIMIT 1;
    """
    params = (email, password)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
    if not row:
        return None
    return BuilderInfo(
        builder_id=row["builder_id"],
        builder_name=row["builder_name"],
        city=row.get("city"),
        payment_history=row["payment_history"],
        total_orders=row["total_orders"],
        total_value=float(row["total_value"]),
    )




def fetch_builder_material_history(
    builder_id: int, material_id: int
) -> Dict[str, Optional[float]]:
    """
    Fetch historical pricing for a specific builder-material pair and overall averages.

    SQL (builder-specific):
    SELECT
        COUNT(*) AS builder_order_count,
        SUM(quantity) AS builder_total_quantity,
        AVG(unit_price) AS builder_avg_unit_price
    FROM sales_history
    WHERE builder_id = %s AND material_id = %s;

    SQL (overall material history, last 3 months):
    SELECT
        AVG(unit_price) AS material_avg_price_3m
    FROM sales_history
    WHERE material_id = %s
      AND sale_date >= CURRENT_DATE - INTERVAL '90 days';
    """
    result: Dict[str, Optional[float]] = {
        "builder_order_count": None,
        "builder_total_quantity": None,
        "builder_avg_unit_price": None,
        "material_avg_price_3m": None,
    }

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Builder-specific history
            cur.execute(
                """
                SELECT
                    COUNT(*) AS builder_order_count,
                    COALESCE(SUM(quantity), 0) AS builder_total_quantity,
                    COALESCE(AVG(unit_price), 0) AS builder_avg_unit_price
                FROM sales_history
                WHERE builder_id = %s AND material_id = %s;
                """,
                (builder_id, material_id),
            )
            row = cur.fetchone()
            if row:
                result["builder_order_count"] = float(row["builder_order_count"])
                result["builder_total_quantity"] = float(row["builder_total_quantity"])
                result["builder_avg_unit_price"] = float(row["builder_avg_unit_price"])

            # Overall material history (last 3 months)
            cur.execute(
                """
                SELECT
                    COALESCE(AVG(unit_price), 0) AS material_avg_price_3m
                FROM sales_history
                WHERE material_id = %s
                  AND sale_date >= CURRENT_DATE - INTERVAL '90 days';
                """,
                (material_id,),
            )
            row = cur.fetchone()
            if row:
                result["material_avg_price_3m"] = float(row["material_avg_price_3m"])

    return result


def fetch_pricing_rules_for_quantity(
    material_id: int, quantity: int
) -> List[Dict[str, Any]]:
    """
    Fetch applicable pricing rules (volume discounts, margin rules) for a quantity.

    SQL:
    SELECT rule_id, min_quantity, max_quantity, discount_percentage,
           rule_type, margin_percentage
    FROM pricing_rules
    WHERE material_id = %s
      AND min_quantity <= %s
      AND (max_quantity IS NULL OR max_quantity >= %s)
    ORDER BY min_quantity ASC;
    """
    query = """
        SELECT rule_id, min_quantity, max_quantity, discount_percentage,
               rule_type, margin_percentage
        FROM pricing_rules
        WHERE material_id = %s
          AND min_quantity <= %s
          AND (max_quantity IS NULL OR max_quantity >= %s)
        ORDER BY min_quantity ASC;
    """
    params = (material_id, quantity, quantity)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchone()

    return dict(rows) if rows else None


def insert_sales_history_record(
    builder_id: int,
    material_id: int,
    quantity: int,
    unit_price: float,
    payment_status: str = "pending",
    delivery_status: str = "pending",
) -> int:
    """
    Insert a new sale record when a deal is finalized.

    SQL:
    INSERT INTO sales_history (
        builder_id, material_id, quantity, unit_price,
        total_amount, sale_date, payment_status, delivery_status
    )
    VALUES (%s, %s, %s, %s, %s, CURRENT_DATE, %s, %s)
    RETURNING sale_id;
    """
    total_amount = unit_price * quantity

    query = """
        INSERT INTO sales_history (
            builder_id, material_id, quantity, unit_price,
            total_amount, sale_date, payment_status, delivery_status
        )
        VALUES (%s, %s, %s, %s, %s, CURRENT_DATE, %s, %s)
        RETURNING sale_id;
    """
    params = (
        builder_id,
        material_id,
        quantity,
        unit_price,
        total_amount,
        payment_status,
        delivery_status,
    )

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            sale_id_row = cur.fetchone()
        conn.commit()

    return int(sale_id_row["sale_id"])

def fetch_alternative_brands(
    material_name: str,
    exclude_brand: Optional[str],
    quantity: int,
) -> List[Dict[str, Any]]:
    """
    Fetch other available brands for the same material (excluding current brand).
    Returns a list sorted by base_cost ASC so cheapest comes first.

    SQL:
    SELECT material_id, material_name, brand, unit, base_cost, stock_quantity
    FROM materials
    WHERE LOWER(material_name) = LOWER(%s)
      AND (%s IS NULL OR LOWER(brand) != LOWER(%s))
      AND stock_quantity >= %s
    ORDER BY base_cost ASC;
    """
    query = """
        SELECT material_id, material_name, brand, unit, base_cost, stock_quantity
        FROM materials
        WHERE LOWER(material_name) = LOWER(%s)
          AND (%s IS NULL OR LOWER(COALESCE(brand, '')) != LOWER(COALESCE(%s, '')))
          AND stock_quantity >= %s
        ORDER BY base_cost ASC;
    """
    params = (material_name, exclude_brand, exclude_brand, quantity)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    return [dict(row) for row in rows] if rows else []

def ensure_schema():
    """
    Utility helper for local development to ensure required tables exist.

    This follows the exact schema from the project brief.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS materials (
                    material_id SERIAL PRIMARY KEY,
                    material_name VARCHAR(100) NOT NULL,
                    brand VARCHAR(100),
                    unit VARCHAR(20),
                    base_cost DECIMAL(10,2) NOT NULL,
                    stock_quantity INTEGER DEFAULT 0,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS builders (
                    builder_id SERIAL PRIMARY KEY,
                    builder_name VARCHAR(200) NOT NULL,
                    contact_number VARCHAR(15),
                    email VARCHAR(100) UNIQUE,
                    password VARCHAR(100) NOT NULL,
                    city VARCHAR(100),
                    payment_history VARCHAR(20) DEFAULT 'good',
                    total_orders INTEGER DEFAULT 0,
                    total_value DECIMAL(12,2) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sales_history (
                    sale_id SERIAL PRIMARY KEY,
                    builder_id INTEGER REFERENCES builders(builder_id),
                    material_id INTEGER REFERENCES materials(material_id),
                    quantity INTEGER NOT NULL,
                    unit_price DECIMAL(10,2) NOT NULL,
                    total_amount DECIMAL(12,2) NOT NULL,
                    sale_date DATE NOT NULL,
                    payment_status VARCHAR(20) DEFAULT 'pending',
                    delivery_status VARCHAR(20) DEFAULT 'pending'
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pricing_rules (
                    rule_id SERIAL PRIMARY KEY,
                    material_id INTEGER REFERENCES materials(material_id),
                    min_quantity INTEGER NOT NULL,
                    max_quantity INTEGER,
                    discount_percentage DECIMAL(5,2),
                    rule_type VARCHAR(50),
                    margin_percentage DECIMAL(5,2) NOT NULL
                );
                """
            )
        conn.commit()

