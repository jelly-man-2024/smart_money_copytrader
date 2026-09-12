CREATE USER IF NOT EXISTS 'smart_money_runtime'@'%'
  IDENTIFIED BY 'local-runtime-only';
GRANT SELECT ON smart_money.copy_relationships TO 'smart_money_runtime'@'%';
FLUSH PRIVILEGES;
