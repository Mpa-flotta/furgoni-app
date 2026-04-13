from werkzeug.security import generate_password_hash

password = input("Inserisci la password admin: ")
print(generate_password_hash(password))
