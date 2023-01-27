CREATE SCHEMA TRZ;
set search_path to TRZ;

create table TRZ.FIN_TRANZACTIONS(tr_id serial PRIMARY KEY,
                                  tr_date timestamp without time zone NOT NULL,
                                  tr_category varchar(200) NOT NULL,
                                  tr_currency varchar(3) NOT NULL,
                                  tr_values float8 NOT NULL,
                                  tr_comment varchar(1000));

INSERT INTO TRZ.FIN_TRANZACTIONS(tr_date, tr_category, tr_currency, tr_values, tr_comment)
	VALUES ('2022-10-01','test','tst', 500, '');
