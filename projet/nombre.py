# On demande les valeurs à l'utilisateur
#a = float(input("Entrez la valeur de a : "))
#b = float(input("Entrez la valeur de b : "))

# On calcule x
#x = -b / a

# On affiche le résultat
#print("La solution de l'équation est x =", x)
# Demander à l'utilisateur de saisir les coefficients a et b
a = float(input("Entrez la valeur de a : "))
b = float(input("Entrez la valeur de b : "))

# Vérifier si a est différent de zéro
if a != 0:
    x = -b / a
    print(f"La solution de l'équation {a}x + {b} = 0 est x = {x}")
else:
    # Cas où a = 0
    if b == 0:
        print("L'équation a une infinité de solutions.")
    else:
        print("L'équation n'a pas de solution.")

