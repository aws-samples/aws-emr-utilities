create user rangeradmin with createdb login password 'rangeradmin';
create database ranger with owner rangeradmin;
GRANT ALL PRIVILEGES ON SCHEMA public TO rangeradmin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO rangeradmin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO rangeradmin;