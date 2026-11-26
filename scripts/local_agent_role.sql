-- Chạy bằng chủ DB trên môi trường local/test, không dùng cho staging.
-- Password do môi trường cấp riêng, không ghi vào repository.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vingo_agent') THEN
        CREATE ROLE vingo_agent LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END $$;
-- Không đụng quyền PUBLIC: DB dùng chung với backend. Role không có quyền bảng nào ngoài schema agent.
REVOKE ALL ON SCHEMA public FROM vingo_agent;
GRANT USAGE ON SCHEMA agent TO vingo_agent;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA agent TO vingo_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA agent GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vingo_agent;
