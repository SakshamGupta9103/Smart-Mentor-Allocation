-- =====================================================================
-- Smart Mentor Allocation — expected MySQL tables for "Sync from MySQL"
-- =====================================================================
-- Run this on the college MySQL server (or make sure your existing tables
-- use these column names), then point the app's "Sync from MySQL" form at
-- the database. Column names are matched case-insensitively and extra
-- columns are ignored. `attendance` and `cgpa` may be left NULL.
--
--   mysql -h HOST -u USER -p COLLEGE_DB < mysql_schema.sql
-- =====================================================================

CREATE TABLE IF NOT EXISTS students (
  roll       VARCHAR(32)  NOT NULL PRIMARY KEY,
  name       VARCHAR(128) NOT NULL,
  programme  VARCHAR(64)  NOT NULL,
  semester   INT          NOT NULL,
  section    VARCHAR(8)   NOT NULL,
  attendance DECIMAL(5,2) NULL,
  cgpa       DECIMAL(3,1) NULL
);

CREATE TABLE IF NOT EXISTS subjects (
  teacher   VARCHAR(128) NOT NULL,
  subject   VARCHAR(128) NOT NULL,
  programme VARCHAR(64)  NOT NULL,
  semester  INT          NOT NULL,
  section   VARCHAR(8)   NOT NULL
);
