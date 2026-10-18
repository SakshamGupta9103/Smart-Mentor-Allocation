-- =====================================================================
-- Seed data for the MySQL sync demo ("Sync from MySQL & allocate").
-- Run ONCE, after creating the database and the tables:
--
--   mysql -u root -e "CREATE DATABASE IF NOT EXISTS mentor_demo;"
--   mysql -u root mentor_demo < mysql_schema.sql
--   mysql -u root mentor_demo < mysql_seed_demo.sql
--
-- Or import both files in phpMyAdmin (XAMPP) into a `mentor_demo` database.
-- Then in the app: host=localhost, port=3306, user=root, password=(empty),
-- database=mentor_demo, tables students / subjects.
-- =====================================================================

INSERT INTO subjects (teacher, subject, programme, semester, section) VALUES
('Dr. Anita Sharma', 'Programming in C',  'BCA', 1, 'A'),
('Dr. Anita Sharma', 'Web Basics',        'BCA', 1, 'A'),
('Prof. Rajesh Kumar', 'Mathematics I',  'BCA', 1, 'A'),
('Dr. Vikram Singh', 'English',          'BCA', 1, 'B');

INSERT INTO students (roll, name, programme, semester, section, attendance, cgpa) VALUES
('BCA1A001', 'Aarav Patel',  'BCA', 1, 'A', 88, 8.2),
('BCA1A002', 'Diya Sharma',  'BCA', 1, 'A', 92, 9.1),
('BCA1A003', 'Arjun Mehta',  'BCA', 1, 'A', 68, 5.4),
('BCA1A004', 'Ishita Rao',   'BCA', 1, 'A', 81, 7.6),
('BCA1A005', 'Kabir Singh',  'BCA', 1, 'A', 95, 8.8),
('BCA1A006', 'Myra Nair',    'BCA', 1, 'A', 74, 6.2),
('BCA1B001', 'Neha Gupta',   'BCA', 1, 'B', 90, 8.5),
('BCA1B002', 'Rohan Verma',  'BCA', 1, 'B', 66, 4.9),
('BCA1B003', 'Sara Khan',    'BCA', 1, 'B', 84, 7.9),
('BCA1B004', 'Vihaan Joshi', 'BCA', 1, 'B', 78, 7.1);
