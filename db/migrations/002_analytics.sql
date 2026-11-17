-- Product analytics tables (onus.api.analytics). The app also creates them on startup.

CREATE TABLE events (
	id VARCHAR(36) NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	visitor VARCHAR(16) NOT NULL, 
	type VARCHAR(32) NOT NULL, 
	path VARCHAR(255), 
	analysis_id VARCHAR(36), 
	props JSONB, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_events_analysis_id ON events (analysis_id);

CREATE INDEX ix_events_session_id ON events (session_id);

CREATE INDEX ix_events_ts ON events (ts);

CREATE INDEX ix_events_type ON events (type);

CREATE INDEX ix_events_visitor ON events (visitor);

CREATE TABLE request_log (
	id VARCHAR(36) NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE NOT NULL, 
	visitor VARCHAR(16) NOT NULL, 
	method VARCHAR(8) NOT NULL, 
	route VARCHAR(128) NOT NULL, 
	path VARCHAR(255) NOT NULL, 
	status INTEGER NOT NULL, 
	ms FLOAT NOT NULL, 
	analysis_id VARCHAR(36), 
	referrer_host VARCHAR(128), 
	client VARCHAR(16), 
	PRIMARY KEY (id)
);

CREATE INDEX ix_request_log_analysis_id ON request_log (analysis_id);

CREATE INDEX ix_request_log_route ON request_log (route);

CREATE INDEX ix_request_log_ts ON request_log (ts);

CREATE INDEX ix_request_log_visitor ON request_log (visitor);
