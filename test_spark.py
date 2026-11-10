import os
import sys


# Mantenemos esto para evitar el error de desconexión del worker de Python
os.environ['PYSPARK_PYTHON'] = sys.executable
os.environ['PYSPARK_DRIVER_PYTHON'] = sys.executable

from pyspark.sql import SparkSession
from pyspark.sql.functions import sum, desc

# 1. Iniciar la sesión de Spark
spark = SparkSession.builder \
    .appName("AnalisisSteamIndie") \
    .getOrCreate()

# 2. Cargar el dataset real
# Cambiamos header=False porque el archivo no tiene nombres de columnas
df_steam = spark.read.csv(
    "steam-200k.csv", 
    header=False, 
    inferSchema=True
)

# Renombrar las columnas manualmente para darles sentido
df_steam = df_steam.toDF("User_ID", "Game_Name", "Action", "Hours_Played", "Other")

# 3. Inspeccionar el dataset
print("--- ESTRUCTURA DEL DATASET (COLUMNAS) ---")
df_steam.printSchema()

print("\n--- PRIMEROS 5 REGISTROS ---")
# truncate=False evita que la consola recorte los textos largos (como las descripciones)
df_steam.show(5, truncate=False) 

# 4. Filtrar solo los registros donde la acción es "play" (ignoramos las filas de "purchase")
df_play = df_steam.filter(df_steam["Action"] == "play")

# 5. Agrupar por nombre de juego, sumar las horas totales y ordenar de mayor a menor
top_games = df_play.groupBy("Game_Name") \
    .agg(sum("Hours_Played").alias("Total_Hours")) \
    .orderBy(desc("Total_Hours"))

print("\n--- TOP 10 JUEGOS CON MÁS HORAS (ENGAGEMENT) ---")
top_games.show(10, truncate=False)

# 6. Cerrar la sesión
spark.stop()