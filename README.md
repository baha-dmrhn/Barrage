# Baha Baraj Doluluk Paneli

Çalıştırmak için:

```powershell
python main.py
```

Tarayıcıdan `http://127.0.0.1:5000` adresini açın. EPİAŞ e-posta ve şifrenizi giriş ekranına yazın. Parola diske, tarayıcı depolamasına veya günlük kayıtlarına yazılmaz; uygulama yalnızca EPİAŞ'ın geçici oturum anahtarını bellek üzerinde tutar.

Render dağıtımında kullanıcı adı veya şifre ortam değişkeni gerekmez. Bilgiler giriş
anında HTTPS üzerinden sunucuya, oradan EPİAŞ'a iletilir. Parola saklanmaz; başarılı
girişten sonra yalnızca geçici EPİAŞ oturum anahtarı sunucu belleğinde tutulur.
