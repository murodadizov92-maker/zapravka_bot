# Gaz rasxod Telegram bot

Guruhda xodimlar yozgan gaz-zapravka summalarini avtomatik saqlaydi va
bugun / kecha / bu oy / sana oralig'i bo'yicha hisobot beradi.

## 1. Botni BotFather orqali yaratish (agar hali yaratmagan bo'lsangiz)
1. Telegramda @BotFather ga yozing → `/newbot`
2. Nom va username bering, tokenni saqlab qo'ying (masalan `123456:ABC-DEF...`)

## 2. Serverga yuklash
```bash
scp -r gaz-bot root@SIZNING_SERVER_IP:/root/
ssh root@SIZNING_SERVER_IP
cd /root/gaz-bot
```

## 3. Python muhitini tayyorlash
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 4. Tokenni sozlash
`bot.py` faylida yoki muhit o'zgaruvchisi orqali:
```bash
export GAZ_BOT_TOKEN="123456:ABC-DEF..."
```
yoki `gaz-bot.service` faylidagi `Environment=GAZ_BOT_TOKEN=...` qatoriga yozing.

## 5. O'zingizning Telegram ID'ingizni bilish (chek tashlaganda hisobga
qo'shilmasligi uchun)
Telegramda @userinfobot ga yozing — u sizga ID raqamingizni beradi. Shu
raqamni `bot.py` ichidagi `ADMIN_IDS = {...}` ro'yxatiga qo'shing.

## 6. Qo'lda sinab ko'rish
```bash
python bot.py
```
Bot konsolda "Bot ishga tushdi (polling rejimida)" deb yozsa — ishlayapti.
`Ctrl+C` bilan to'xtating.

## 7. 24/7 ishlashi uchun systemd'ga ulash
```bash
cp gaz-bot.service /etc/systemd/system/gaz-bot.service
# fayl ichidagi WorkingDirectory, ExecStart va tokenni tekshiring
systemctl daemon-reload
systemctl enable gaz-bot
systemctl start gaz-bot
systemctl status gaz-bot   # ishlab turganini tekshirish
```

Bot shu tarzda **doimiy** (polling) rejimida ishlaydi — har 5 daqiqada
qayta ishga tushirishning hojati yo'q. `Restart=always` tufayli agar bot
biror sababdan yiqilib qolsa, systemd uni 5 soniyadan keyin o'zi qayta
ko'taradi.

## 8. Guruhga qo'shish
1. Botni Telegram guruhingizga qo'shing
2. Guruh sozlamalarida botga admin huquqi bermasangiz ham bo'ladi — u
   faqat xabarlarni o'qiydi (agar guruhda "Group Privacy" yoqilgan bo'lsa,
   @BotFather'da `/setprivacy` → `Disable` qiling, aks holda bot oddiy
   xabarlarni ko'ra olmaydi)

## 9. Ishlatish
- Xodim guruhga masalan `50000` yoki `50 ming kerak` deb yozsa — bot uni
  jim saqlab qo'yadi (javob qaytarmaydi, guruhni "iflos" qilmaydi)
- Siz (yoki istalgan a'zo) `/hisobot` deb yozsa — tugmalar chiqadi:
  **Bugun / Kecha / Bu oy / Sana oralig'i**
- Sana oralig'ini tanlasangiz, bot ketma-ket ikkita sanani so'raydi
  (masalan `01.09.2026` va `26.09.2026`)

## Cheklovlar / keyin yaxshilash mumkin bo'lgan joylar
- Summani matndan ajratib olish oddiy qoidalar asosida ishlaydi (raqam +
  ixtiyoriy "ming/mln" so'zi). Juda g'alati yozilgan xabarlarda (masalan
  faqat harflar bilan "ellik ming") aniqlamasligi mumkin — shunday holat
  ko'p uchrasa, ayting, kengaytiraman
- Hozircha ma'lumotlar shu server ichidagi `gaz_rasxod.db` (SQLite)
  faylida saqlanadi — serverni almashtirsangiz shu faylni ham ko'chiring
