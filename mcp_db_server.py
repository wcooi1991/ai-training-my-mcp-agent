
import json
import os
import sqlite3
import sys
import sqlparse

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

mcp = MCPServer("BankingSQLiteMCPServer")

DB_FILE = sys.argv[1] if len(sys.argv) > 1 else "bank11.sqlite"

def ensure_database():
    """Auto-bootstrap sample banking database if not already present."""
    if not os.path.exists(DB_FILE):
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS branches (
            branch_id TEXT PRIMARY KEY,
            branch_name TEXT NOT NULL,
            city TEXT NOT NULL,
            state TEXT NOT NULL,
            region TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS customers (
            customer_id TEXT PRIMARY KEY,
            branch_id TEXT NOT NULL,
            full_name TEXT NOT NULL,
            customer_segment TEXT NOT NULL,
            join_date TEXT NOT NULL,
            FOREIGN KEY (branch_id) REFERENCES branches(branch_id)
        );
        CREATE TABLE IF NOT EXISTS accounts (
            account_id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            account_type TEXT NOT NULL,
            balance REAL NOT NULL,
            currency TEXT DEFAULT 'MYR',
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
        );
        INSERT OR IGNORE INTO branches VALUES
            ('BR_001', 'Petaling Jaya Main', 'Petaling Jaya', 'Selangor', 'Central'),
            ('BR_002', 'Kepong Metro Prima', 'Kuala Lumpur', 'W.P. Kuala Lumpur', 'Central'),
            ('BR_003', 'KLCC Corporate Banking', 'Kuala Lumpur', 'W.P. Kuala Lumpur', 'Central'),
            ('BR_004', 'Georgetown Heritage', 'George Town', 'Penang', 'Northern'),
            ('BR_005', 'Johor Bahru City Square', 'Johor Bahru', 'Johor', 'Southern');
        INSERT OR IGNORE INTO customers VALUES
            ('CUST_901', 'BR_001', 'Tan Wei Ming', 'Premier Banking', '2023-01-15'),
            ('CUST_902', 'BR_001', 'Ahmad Farhan', 'Mass Affluent', '2023-03-22'),
            ('CUST_903', 'BR_001', 'Siti Nurhaliza', 'Mass Retail', '2024-06-10'),
            ('CUST_904', 'BR_001', 'Kavitha Pillai', 'Premier Banking', '2024-08-01'),
            ('CUST_905', 'BR_002', 'Chong Chee Keong', 'Mass Retail', '2022-11-05'),
            ('CUST_906', 'BR_002', 'Faridah binti Osman', 'Mass Affluent', '2023-09-19'),
            ('CUST_907', 'BR_002', 'Lee Kuan Yew', 'Premier Banking', '2024-02-14'),
            ('CUST_908', 'BR_003', 'Goh Chok Tong', 'Private Wealth', '2021-05-18'),
            ('CUST_909', 'BR_003', 'Zainal Abidin', 'Corporate Banking', '2022-04-30'),
            ('CUST_910', 'BR_004', 'Lim Guan Eng', 'Mass Affluent', '2023-07-09');
        INSERT OR IGNORE INTO accounts VALUES
            ('ACC_1001', 'CUST_901', 'Savings', 45200.50, 'MYR'),
            ('ACC_1002', 'CUST_902', 'Current', 12340.00, 'MYR'),
            ('ACC_1003', 'CUST_903', 'Savings', 6800.75, 'MYR'),
            ('ACC_1004', 'CUST_904', 'Fixed Deposit', 150000.00, 'MYR'),
            ('ACC_1005', 'CUST_905', 'Savings', 3200.00, 'MYR'),
            ('ACC_1006', 'CUST_906', 'Current', 28900.20, 'MYR'),
            ('ACC_1007', 'CUST_907', 'Fixed Deposit', 300000.00, 'MYR'),
            ('ACC_1008', 'CUST_908', 'Investment', 1250000.00, 'MYR'),
            ('ACC_1009', 'CUST_909', 'Corporate Current', 489000.00, 'MYR'),
            ('ACC_1010', 'CUST_910', 'Savings', 9800.00, 'MYR');
        """)
        conn.commit()
        conn.close()

def is_sql_mutation(sql_query: str) -> bool:
    cleaned = sql_query.strip().strip(";")
    parsed = sqlparse.parse(cleaned)
    if not parsed:
        return False
    forbidden = {'DROP', 'DELETE', 'UPDATE', 'INSERT', 'ALTER', 'TRUNCATE', 'PRAGMA', 'REPLACE', 'CREATE'}
    for stmt in parsed:
        if stmt.get_type() != 'SELECT':
            return True
        for token in stmt.flatten():
            if token.value.upper() in forbidden:
                return True
    return False

@mcp.tool()
def list_tables() -> str:
    """Discover all currently available tables in the banking database."""
    ensure_database()
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
    tables = [row[0] for row in cur.fetchall()]
    conn.close()
    return json.dumps({"status": "success", "tables": tables})

@mcp.tool()
def describe_table(table_name: str) -> str:
    """Inspect column names, types, and primary keys for a specified table."""
    ensure_database()
    cleaned = table_name.strip().strip("'").strip('"')
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({cleaned});")
    cols = [{"cid": r[0], "name": r[1], "type": r[2], "pk": r[5]} for r in cur.fetchall()]
    conn.close()
    if not cols:
        return json.dumps({"status": "error", "message": f"Table '{cleaned}' not found."})
    return json.dumps({"status": "success", "table": cleaned, "columns": cols})

@mcp.tool()
def read_query(sql_query: str) -> str:
    """Execute a read-only SQL SELECT query against the banking database."""
    ensure_database()
    if is_sql_mutation(sql_query):
        return json.dumps({"status": "blocked", "error": "Mutation queries (DROP, DELETE, UPDATE) are prohibited."})
    try:
        conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(sql_query.strip())
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        conn.close()
        return json.dumps({"status": "success", "columns": columns, "rows": [list(r) for r in rows[:50]], "count": len(rows)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def drop_table_admin(table_name: str) -> str:
    """Admin-only destructive tool. Should be stripped by ADK tool_filter!"""
    return f"DANGER: Attempted to drop {table_name}"

if __name__ == "__main__":
    ensure_database()
    mcp.run(transport="stdio")
