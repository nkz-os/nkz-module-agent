-- 000 — Create the module's least-privilege role.
--
-- Deliberately no password here: a password in a public repository would be a
-- secret leak. The password is assigned out-of-band in the deployment
-- environment (SealedSecret in the private gitops overlay).
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_module') THEN
    CREATE ROLE agent_module LOGIN;
  END IF;
END $$;
