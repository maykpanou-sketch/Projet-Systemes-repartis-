
secondes = int(input("Entrez le nombre de secondes : "))


heures = secondes // 3600
reste = secondes % 3600
minutes = reste // 60
secondes_restantes = reste % 60


print(f"{heures} heure, {minutes} minute et {secondes_restantes} seconde")

nombre = 421

print(nombre * 1, nombre * 2, nombre * 3, nombre * 4, nombre * 5)
