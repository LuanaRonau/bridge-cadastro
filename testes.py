from selenium import webdriver
import time

# abrir navegador
navegador = webdriver.Chrome()

#acessar site de cadastros
navegador.get("https://desafio-ps-qa.bridge.ufsc.br/cadastro")

# colocar o navegador em tela cheia
navegador.maximize_window()

# página de login
campo_usuario = navegador.find_element("id", "usuario")
campo_usuario.click()
campo_usuario.send_keys("luana.ronau.m@gmail.com")

campo_password = navegador.find_element("id", "password")
campo_password.click()
campo_password.send_keys("QRMevGS6sPzkGL63MhEgoO")

checkbox_termos_uso = navegador.find_element("id", "termos-de-uso")
checkbox_termos_uso.click()

btn_acessar = navegador.find_element("class name", "btn-acessar")
btn_acessar.click()

# página de orientações

btn_iniciar_desafio = navegador.find_element("class name", "btn-acessar")
navegador.execute_script("arguments[0].scrollIntoView()", btn_iniciar_desafio)
time.sleep(3)
btn_iniciar_desafio = navegador.find_element("class name", "btn-acessar")
btn_iniciar_desafio.click()

# página de cadastros

# campos
campo_cpf = navegador.find_element("id", "cpf")
campo_cns = navegador.find_element("id", "cns")
campo_nome_completo = navegador.find_element("id", "nome-completo")
campo_data_nascimento = navegador.find_element("id", "data-nascimento")
campo_sexo = navegador.find_element("id", "sexo")
campo_telefone_residencial = navegador.find_element("id", "telefone-residencial")
campo_telefone_celular = navegador.find_element("id", "telefone-celular")

# botão salvar
btn_salvar = navegador.find_element("class name", "btn-salvar")

# teste CPF
campo_cpf.send_keys("")
btn_salvar.click()
time.sleep(10)

# Código incompleto por motivos de tempo