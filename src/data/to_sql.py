import pandas as pd
import sqlalchemy
from src import config, utils

DB_STRING = f'postgresql+psycopg2://{config.DB_USER}:{utils.get_secrets("SQL_PASS")}@0.0.0.0:5432/{config.DB_NAME}'
print(DB_STRING)
engine = sqlalchemy.create_engine(DB_STRING)

df = pd.read_sql("""select * from TRZ.FIN_TRANZACTIONS""", engine)
print(df.head())