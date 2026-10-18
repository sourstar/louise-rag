"""A dump run's `page_props` table for the wikipedia fixtures (parser 5),
written the way dumps.wikimedia.org's are: MariaDB's header, then extended
INSERT statements of `(pp_page,'pp_propname','pp_value',pp_sortkey)` in
primary-key order (the header copied from trwiki-20260801-page_props.sql.gz).
"""
from __future__ import annotations

import gzip
from pathlib import Path

from app.plugins.wikipedia import page_props

HEADER = b"""-- MariaDB dump 10.19  Distrib 10.5.29-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: dbstore1007.eqiad.wmnet    Database: trwiki
-- ------------------------------------------------------
-- Server version	10.11.13-MariaDB-log

/*!40101 SET NAMES utf8mb4 */;

--
-- Table structure for table `page_props`
--

DROP TABLE IF EXISTS `page_props`;
CREATE TABLE `page_props` (
  `pp_page` int(10) unsigned NOT NULL,
  `pp_propname` varbinary(60) NOT NULL DEFAULT '',
  `pp_value` blob NOT NULL,
  `pp_sortkey` float DEFAULT NULL,
  PRIMARY KEY (`pp_page`,`pp_propname`)
) ENGINE=InnoDB DEFAULT CHARSET=binary ROW_FORMAT=COMPRESSED;

--
-- Dumping data for table `page_props`
--

/*!40000 ALTER TABLE `page_props` DISABLE KEYS */;
"""
FOOTER = b"/*!40000 ALTER TABLE `page_props` ENABLE KEYS */;\n"


def tuple_of(page: int, name: str, value: str, sortkey: str = "NULL") -> bytes:
    esc = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"({page},'{name}','{esc}',{sortkey})".encode("utf-8")


def write(dump: Path, rows=(), *, per_statement: int = 1000) -> Path:
    """`<run>-page_props.sql.gz` beside `dump`: `rows` are (page, name, value)
    or (page, name, value, sortkey), sorted here into primary-key order."""
    rows = sorted(rows, key=lambda r: (r[0], r[1]))
    body = b""
    for i in range(0, len(rows), per_statement):
        part = rows[i:i + per_statement]
        body += (b"INSERT INTO `page_props` VALUES "
                 + b",".join(tuple_of(*r) for r in part) + b";\n")
    path = page_props.props_path_of(dump)
    path.write_bytes(gzip.compress(HEADER + body + FOOTER))
    return path
