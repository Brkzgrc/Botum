from fpdf import FPDF

FONT_DIR = "/usr/share/fonts/truetype/liberation/"

class PDF(FPDF):
    def __init__(self):
        super().__init__()
        self.add_font("Sans", "",  FONT_DIR + "LiberationSans-Regular.ttf")
        self.add_font("Sans", "B", FONT_DIR + "LiberationSans-Bold.ttf")
        self.add_font("Sans", "I", FONT_DIR + "LiberationSans-Italic.ttf")
        self.add_font("Sans", "BI",FONT_DIR + "LiberationSans-BoldItalic.ttf")
        self.add_font("Mono", "",  FONT_DIR + "LiberationMono-Regular.ttf")

    def header(self):
        self.set_font("Sans", "I", 8)
        self.set_text_color(130, 130, 130)
        self.cell(0, 7, "Botum Sistemi — Sistem Dokümantasyonu", align="L")
        self.ln(1)
        self.set_draw_color(200, 200, 200)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-12)
        self.set_font("Sans", "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 6, f"Sayfa {self.page_no()}", align="C")

    def chapter_title(self, num, title):
        self.set_font("Sans", "B", 13)
        self.set_fill_color(25, 25, 50)
        self.set_text_color(255, 255, 255)
        self.cell(0, 9, f"  {num}. {title}", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(3)

    def section_title(self, title):
        self.ln(2)
        self.set_font("Sans", "B", 10.5)
        self.set_text_color(25, 80, 170)
        self.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.set_draw_color(25, 80, 170)
        self.line(self.l_margin, self.get_y(), self.l_margin + 55, self.get_y())
        self.set_draw_color(0, 0, 0)
        self.ln(2)

    def sub_title(self, title):
        self.set_font("Sans", "B", 9.5)
        self.set_text_color(50, 50, 50)
        self.cell(0, 6, title, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def body(self, txt):
        self.set_x(self.l_margin)
        self.set_font("Sans", "", 9.5)
        self.multi_cell(0, 5.5, txt)
        self.ln(1)

    def bullet(self, txt, indent=8):
        self.set_font("Sans", "", 9.5)
        x0 = self.l_margin + indent
        w_avail = self.w - self.r_margin - x0 - 5
        self.set_x(x0)
        self.cell(5, 5.5, "•")
        self.multi_cell(w_avail, 5.5, txt)

    def kv(self, key, val):
        x0 = self.l_margin + 8
        w_key = 52
        w_val = self.w - self.r_margin - x0 - w_key
        self.set_font("Sans", "B", 9.5)
        self.set_x(x0)
        self.cell(w_key, 5.5, key + ":")
        self.set_font("Sans", "", 9.5)
        self.multi_cell(w_val, 5.5, val)

    def info_box(self, txt, color=(235, 245, 255)):
        self.set_x(self.l_margin)
        self.set_fill_color(*color)
        self.set_font("Sans", "I", 9)
        self.multi_cell(0, 5.5, txt, fill=True)
        self.set_fill_color(255, 255, 255)
        self.ln(2)

    def divider(self):
        self.ln(3)
        self.set_draw_color(180, 180, 180)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.set_draw_color(0, 0, 0)
        self.ln(5)

pdf = PDF()
pdf.set_auto_page_break(True, margin=14)
pdf.add_page()

# ── KAPAK ──────────────────────────────────────────────────────────────────
pdf.set_font("Sans", "B", 22)
pdf.set_text_color(20, 20, 20)
pdf.ln(6)
pdf.cell(0, 13, "BOTUM SİSTEMİ", align="C", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Sans", "", 13)
pdf.set_text_color(80, 80, 80)
pdf.cell(0, 8, "Tam Sistem Dokümantasyonu", align="C", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Sans", "I", 9)
pdf.set_text_color(150, 150, 150)
pdf.cell(0, 6, "Haziran 2026 — Sunum Amaçlı", align="C", new_x="LMARGIN", new_y="NEXT")
pdf.set_text_color(0, 0, 0)
pdf.ln(6)
pdf.set_draw_color(25, 80, 170)
pdf.set_line_width(0.6)
pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
pdf.set_line_width(0.2)
pdf.set_draw_color(0, 0, 0)
pdf.ln(9)

# ── BÖLÜM 1 ────────────────────────────────────────────────────────────────
pdf.chapter_title(1, "Sistemin Genel Amacı")
pdf.body(
    "Bu sistem, Binance spot piyasasında çalışan, tamamen otomatik bir kripto para sinyal ve "
    "takip platformudur. Üç temel işlevi vardır:\n\n"
    "SİNYAL ÜRETME  →  AI DEĞERLENDİRME  →  POZİSYON TAKİBİ"
)
pdf.ln(2)
pdf.body("Sistem şu anda dört farklı hizmet olarak Render.com üzerinde çalışmaktadır:")
pdf.bullet("bot.py — Anlık düşüş ve momentum sinyalleri")
pdf.bullet("SMC.py — Yapısal kırılım ve trend dönüş sinyalleri")
pdf.bullet("portfolio_tracker.py — Tüm sinyalleri takip eden web paneli")
pdf.bullet("claude_analyzer.py — Her sinyali yapay zeka ile değerlendiren modül (portfolio tracker içinde çalışır)")
pdf.divider()

# ── BÖLÜM 2 ────────────────────────────────────────────────────────────────
pdf.chapter_title(2, "Sinyal Üreticisi: bot.py")
pdf.body(
    "Binance'teki 1000'den fazla USDT çiftini 1 saatlik mumlar bazında izler. "
    "Her saat kapanışında tüm coinleri tarar ve 5 farklı stratejiyle sinyal arar."
)

pdf.section_title("Strateji 1 — PANİK PUMP")
pdf.sub_title("Nedir?")
pdf.body("Piyasada panik satışı sonrası yaşanan ani geri dönüşleri yakalar.")
pdf.sub_title("Ne zaman tetiklenir?")
pdf.body(
    "Bir coin, tek bir 1 saatlik mumda yüzde 7 ila 15 arasında düşer ve bu düşüş "
    "anormal bir işlem hacmiyle gerçekleşirse. Yani piyasada panik var, ama bu panik "
    "manipülatif değil — gerçek bir kapitülasyon."
)
pdf.sub_title("Ekstra kontroller:")
pdf.bullet("Mumun şekli manipülasyon işareti taşımamalı (aşırı wick, ardı ardına düşüş)")
pdf.bullet("Coinin son 5 saatte zaten yüzde 4'ten fazla düşmemiş olması gerekir")
pdf.sub_title("Hedef ve Performans:")
pdf.kv("Stop", "-%3")
pdf.kv("TP1 / TP2 / TP3", "+%5 / +%10 / +%15")
pdf.kv("Geçmiş Başarı Oranı", "~%84  —  Sistemin en güvenilir stratejisi")
pdf.ln(3)

pdf.section_title("Strateji 2 — T24")
pdf.info_box(
    "MEVCUT DURUM: DEVRE DIŞI. Geriye dönük test %10 başarı oranı ve ortalama -%1.7 getiri "
    "gösterdi. Para kaybettirdiği kesinleşince tamamen kapatıldı."
)

pdf.section_title("Strateji 3 — ORTA VADE T72")
pdf.sub_title("Nedir?")
pdf.body(
    "Kısa vadeli ivme kazanan, ama ana trend çizgisinin altında kalan coinleri hedefler. "
    "3 günlük hareket beklentisiyle girilir."
)
pdf.sub_title("Tetiklenme Koşulları (Hepsi gerçekleşmeli):")
pdf.bullet("Son 5 saatte en az +%2.74 yukarı hareket")
pdf.bullet("Hâlâ EMA21'in -%2.74 altında")
pdf.bullet("En yüksek seviyesinden en az -%26.8 aşağıda")
pdf.bullet("200 günlük hareketli ortalama yukarı eğimde")
pdf.sub_title("Hedef ve Performans:")
pdf.kv("Stop / TP", "-%5  |  TP: +%10")
pdf.kv("Başarı Oranı", "%54")
pdf.ln(3)

pdf.section_title("Strateji 4 — UZUN VADE T168")
pdf.sub_title("Nedir?")
pdf.body(
    "Uzun vadeli trendi güçlü olan ama kısa vadede geri çekilmiş coinleri yakalar. "
    "7 günlük hareket beklentisiyle girilir."
)
pdf.sub_title("Tetiklenme Koşulları (Hepsi gerçekleşmeli):")
pdf.bullet("200 günlük ortalamanın üstünde ama 50 günlük ortalamanın -%5 altında")
pdf.bullet("Son 10 saatte +%3.94 yukarı hareket")
pdf.bullet("Son yaklaşık 28 gün içinde zirve görmüş olmalı")
pdf.sub_title("Hedef ve Performans:")
pdf.kv("Stop / TP", "-%8  |  TP: +%25")
pdf.kv("Başarı Oranı", "%40")
pdf.ln(3)

pdf.section_title("Strateji 5 — PUMP PROBABILITY")
pdf.sub_title("Nedir?")
pdf.body("Fiyat sıkıştıktan sonra direnç kıran coinleri önceden tespit eder.")
pdf.sub_title("Tetiklenme Koşulları (Dört koşulun TAMAMI gerçekleşmeli):")
pdf.bullet("Fiyat son dönemde bant içinde sıkışmış (Bollinger Bandı daralması)")
pdf.bullet("Piyasa momentumu artıyor ve alıcılar baskın (ADX yükseliyor, yön alıcı tarafta)")
pdf.bullet("Para girişi gerçekleşiyor (OBV ortalamasının üstünde ve artıyor)")
pdf.bullet("Bir direnç seviyesi yüksek hacimle kırılmış")
pdf.sub_title("Hedef:")
pdf.kv("Stop / TP", "~-%5  |  TP1: +%8  |  TP2: +%15  |  TP3: +%25")
pdf.info_box("NOT: Bu strateji için henüz yeterli gerçek veri birikimi yok. Haziran 2026'dan itibaren veri toplanmaktadır.")

pdf.ln(2)
pdf.sub_title("Tüm Stratejiler İçin Geçerli Filtreler:")
pdf.bullet("BTC 4 saatlik grafiğinde ciddi düşüş trendi varsa hiçbir sinyal üretilmez")
pdf.bullet("Aynı coinde aynı strateji 4 saat içinde tekrar tetiklenmez")
pdf.bullet("24 saatlik işlem hacmi 1 milyon dolar altındaki coinler hiç değerlendirilmez")
pdf.divider()

# ── BÖLÜM 3 ────────────────────────────────────────────────────────────────
pdf.chapter_title(3, "Yapısal Sinyal Üreticisi: SMC.py")
pdf.body(
    "bot.py anlık anomalilere odaklanırken SMC.py tamamen farklı bir felsefeyle çalışır: "
    "piyasanın yapısal 'dili'ni okur. Zeki para hareketlerini (Smart Money Concepts) "
    "takip ederek kurumsal düzeyde giriş noktalarını tespit eder."
)

pdf.section_title("İndirim Bölgesi (Discount Zone)")
pdf.body(
    "Sistem her coin için büyük swing yüksek ve düşüklerini hesaplar, bu aralığın alt "
    "yüzde 55'ini 'indirim bölgesi' olarak tanımlar. Bir coin bu bölgeye girdiğinde "
    "sistem diken kulağa geçer."
)
pdf.sub_title("Aşama 1 — Bildirim (Phase 1):")
pdf.body(
    "Coin indirim bölgesinde, RSI 30'un altında ve derinliği en az yüzde 85 ise "
    "kullanıcıya sadece bilgi mesajı gider. Portfolio'ya kayıt yok, sinyal yok — "
    "sadece 'bu coin izlenmeye değer' uyarısı."
)
pdf.sub_title("Aşama 2 — Gerçek Sinyal (Phase 2):")
pdf.body(
    "Sistem artık o coin'in 'dönüş momentu'nu bekler. Fiyat indirim bölgesinden çıkarken "
    "5 saatlik mikro swing'de CHoCH (Change of Character — Karakter Değişimi) yaşanırsa, "
    "yani fiyat önceki bir swing yüksek seviyesini kırarsa, bu gerçek bir giriş sinyalidir."
)

pdf.section_title("CHoCH ve BOS Farkı")
pdf.kv("CHoCH (Guclu — Trend Dondu)", "Coin dusus trendindeyken ilk kez yukari kirilim yapar.")
pdf.kv("BOS (Orta — Trend Devam)", "Coin zaten yukari trendde, yeni bir yuksek kirar.")

pdf.section_title("Giriş ve Çıkış Yapısı")
pdf.kv("Giriş", "Kırılan swing yüksek seviyesinin tam üzeri (CHoCH çizgisi)")
pdf.kv("Stop", "Son swing dibin yüzde 0.5 altı")
pdf.kv("TP1  (Risk ×1.0)", "Risk mesafesinin 1 katı — pozisyonun YARISI burada kapatılır")
pdf.kv("TP2  (Risk ×2.0)", "Risk mesafesinin 2 katı — kalan kısım için hedef")
pdf.kv("TP3  (Risk ×3.0)", "Risk mesafesinin 3 katı — sadece gölge izleme, gerçek çıkış yok")

pdf.section_title("4H Doğrulama Filtreleri")
pdf.body("Sinyal üretmeden önce iki ek kontrol yapılır. İkisinden BİRİ bile başarısız olursa sinyal gitmez:")
pdf.bullet("Coinin 4 saatlik yapısında swing trendi bearish (aşağı) ise engellenir")
pdf.bullet("Coinin son fiyatı 4 saatlik EMA21'in altındaysa engellenir")

pdf.section_title("BTC Makro Filtreleri")
pdf.bullet("BTC 4 saatlik mumda -%3'ten fazla düşüyorsa hiçbir sinyal üretilmez")
pdf.bullet("BTC'nin son iki 4H mumu EMA21 altında kapanmış ve aşağı eğimliyse sistem tamamen duraklar")
pdf.divider()

# ── BÖLÜM 4 ────────────────────────────────────────────────────────────────
pdf.chapter_title(4, "Yapay Zeka Değerlendirici: claude_analyzer.py")
pdf.body(
    "Her sinyal geldikten sonra — kaynağı bot.py veya SMC.py fark etmeksizin — "
    "bu modül devreye girer. Amaç, sinyali bağlamsal olarak değerlendirip "
    "'bu işleme gireyim mi?' sorusunu yanıtlamak."
)

pdf.section_title("Toplanan Veri (Paralel Olarak)")
pdf.bullet("Coinin 1H, 4H ve günlük grafiklerindeki RSI, EMA50, EMA200, hacim trendi")
pdf.bullet("BTC'nin 1H ve 4H durumu")
pdf.bullet("BTC'nin 4 yıllık geçmişi üzerinden hesaplanan makro seviyeler")
pdf.bullet("200 haftalık ortalama, Fibonacci seviyeleri, haftalık pivot destek/direnç")
pdf.bullet("Fibonacci Bollinger Bandı ve SSL Hybrid — büyük ölçekli trend göstergeleri")
pdf.bullet("Fear & Greed endeksi (piyasa genel duygusu: 0-100 arası)")
pdf.bullet("BTC dominansı ve son 30 günlük trendi")
pdf.bullet("Son 48 saatteki likidite tuzağı (wick) hareketleri")

pdf.section_title("Karar Formatı")
pdf.body("Tüm bu veriyi Claude yapay zekasına gönderir ve üç karardan birini almasını ister:")
pdf.kv("GIR", "Kosullar uygun, giris yapilabilir")
pdf.kv("DIKKAT", "Belirsizlik var, dikkatli olunmali")
pdf.kv("RISKLI", "Girilmemeli")
pdf.body("Karar, gerekçe ve varsa uyarı ile birlikte ayrı bir Telegram botundan gönderilir.")

pdf.section_title("Hafıza Sistemi (Arşiv)")
pdf.body(
    "Her değerlendirme bir arşive kaydedilir. Arşivde o andaki piyasa koşulları da "
    "saklanır: F&G değeri, BTC durumu, dominans. Sinyal kapanınca sonuç (kazanç/kayıp/sona "
    "erdi) arşive işlenir. Sonraki sinyallerde 'bu koşullara benzer geçmişte ne oldu?' "
    "sorusu cevaplanarak Claude'a ek bağlam sağlanır. Yani sistem zamanla geçmiş "
    "deneyiminden öğrenmeye başlar."
)

pdf.section_title("Proaktif Piyasa İzleme")
pdf.body(
    "Sadece sinyal gelince değil, belirli aralıklarla da çalışır. F&G endeksi 10 puan "
    "değişirse, dominans 1.5 puan hareket ederse veya BTC belirgin hareket yaşarsa "
    "Telegram'a rapor gönderir. Ayrıca her gün bağımsız bir piyasa özeti yayınlar."
)
pdf.divider()

# ── BÖLÜM 5 ────────────────────────────────────────────────────────────────
pdf.chapter_title(5, "Pozisyon Takipçisi: portfolio_tracker.py")
pdf.body(
    "Tüm sinyalleri tek bir web panelinde toplar, 5 dakikada bir Binance'ten fiyat "
    "çekerek pozisyonları takip eder ve otomatik çıkış kurallarını uygular."
)

pdf.section_title("Bot Sinyalleri İçin Yaşam Döngüsü")
pdf.body("Pozisyon açılır — fiyat düzenli kontrol edilir — şu koşullardan biri gerçekleşene kadar açık kalır:")
pdf.bullet("TP2 hedefine ulaşıldı → kazançlı kapanış")
pdf.bullet("Trailing stop tetiklendi (zirvenin -%3 altına inildi) → trailing kapanış")
pdf.bullet("Süre doldu (PANİK 24s, T72 72s, T168 168s) → sona erdi")
pdf.body("NOT: TP1'e ulaşmak kapanış değil, sadece bir milestone'dur — sistem kaydeder, pozisyon açık kalır.")

pdf.section_title("SMC Sinyalleri İçin İki Aşamalı Çıkış")
pdf.sub_title("Birinci Aşama (TP1'e kadar):")
pdf.bullet("Stop'a düşülürse direkt kayıp")
pdf.bullet("TP1'e ulaşılırsa pozisyonun yarısı kapatılmış sayılır, 'half_open' moduna geçer")
pdf.sub_title("İkinci Aşama (TP1'den TP2'ye):")
pdf.bullet("Kalan yarım pozisyon TP2'yi bekler")
pdf.bullet("Bu süreçte zirvenin -%2.5 altına inilirse trailing stop tetiklenir")
pdf.bullet("72 saat içinde TP2'ye ulaşılmazsa 'sona erdi' olarak kapanır")
pdf.sub_title("TP3 Gölge Takibi:")
pdf.body(
    "TP2'ye ulaşılınca para çıkmıştır ama sistem TP3 seviyesini izlemeye devam eder. "
    "Amaç: 'ne kadar gidebilirdi?' sorusunu ileride veriyle cevaplayabilmek. "
    "TP3'e ulaşılırsa 'başarılı', -%2.5 altına düşülürse veya 72 saat geçerse 'durdu' yazılır."
)

pdf.section_title("Web Panelinin İçeriği")
pdf.bullet("Açık pozisyonlar: fiyat, kazanç/kayıp, zirve, dip, stop seviyesi, TP hedefleri, geçen süre, AI kararı")
pdf.bullet("Kapanmış sinyaller: sonuç, kazanç, zirve, TP1 tuttu mu, TP3 ne oldu, AI kararı")
pdf.bullet("Performans istatistikleri: toplam, kazanç, kayıp, sona erdi, WR%, ortalama zirve, toplam P&L")
pdf.bullet("Sistem bazlı ayrım: SMC vs Bot")
pdf.bullet("AI performansı: 'GİR dediğinde WR ne, RİSKLİ dediğinde WR ne?'")
pdf.bullet("Alternatif senaryo simülasyonu: 'Sabit TP ve stop kullansaydık ne olurdu?'")
pdf.bullet("Günlük/haftalık/aylık özet")
pdf.divider()

# ── BÖLÜM 6 ────────────────────────────────────────────────────────────────
pdf.chapter_title(6, "Sistemler Arası Bağlantı Haritası")
pdf.body("Dört servis birbirleriyle aşağıdaki şekilde iletişir:")
pdf.ln(2)
pdf.set_font("Mono", "", 8)
pdf.set_fill_color(245, 245, 250)
pdf.set_draw_color(180, 180, 200)
diagram = (
    "bot.py\n"
    "  → Sinyal → Telegram (TELEGRAM_TOKEN)\n"
    "  → Sinyal → portfolio_tracker /api/signal       (kayıt)\n"
    "  → Analiz → portfolio_tracker /api/analyze      (AI talebi)\n\n"
    "SMC.py\n"
    "  → Sinyal → Telegram (TELEGRAM_TOKEN)\n"
    "  → Sinyal → portfolio_tracker /api/signal       (kayıt)\n"
    "  → Analiz → portfolio_tracker /api/analyze      (AI talebi)\n\n"
    "portfolio_tracker\n"
    "  → AI Analiz → claude_analyzer.process_and_send (kendi içinde çalıştırır)\n"
    "  → AI Kararı → Telegram (ANALYZER_TELEGRAM_TOKEN)\n"
    "  → AI Kararı → /api/signal/{id}/analyzer PATCH  (kayıt)\n"
    "  → Sonuç    → claude_analyzer.update_archive    (arşiv güncelleme)"
)
pdf.multi_cell(0, 5, diagram, fill=True, border=1)
pdf.set_font("Sans", "", 9.5)
pdf.set_fill_color(255, 255, 255)
pdf.set_draw_color(0, 0, 0)
pdf.divider()

# ── BÖLÜM 7 ────────────────────────────────────────────────────────────────
pdf.chapter_title(7, "Güçlü Yönler")
pdf.bullet(
    "PANİK PUMP güvenilirliği yüksek. ~%84 başarı oranıyla en test edilmiş strateji. "
    "Tanımlı parametreler, net risk yönetimi."
)
pdf.bullet(
    "SMC yapısal mantığı sağlam. İki aşamalı onay sistemi (Discount Zone + CHoCH + 4H teyit + EMA21) "
    "anlık gürültüyü iyi filtreler."
)
pdf.bullet(
    "AI entegrasyonu gerçek anlam taşıyor. Tek bir indikatöre değil, çoklu zaman dilimleri + makro "
    "bağlam + geçmiş arşiv + likidite sweep'e birlikte bakan bir değerlendirici."
)
pdf.bullet(
    "Portfolio tracker her şeyi takip ediyor. Gerçek veri birikiyor, AI kararlarının performansı "
    "karşılaştırılabilir durumda."
)
pdf.divider()

# ── BÖLÜM 8 ────────────────────────────────────────────────────────────────
pdf.chapter_title(8, "Zayıf Yönler ve Bilinen Sorunlar")
pdf.bullet(
    "AI karar hassasiyeti sorunlu. Prompt kalibrasyonu henüz olgunlaşmamış. Her sinyale riskli "
    "demesi AI'yı etkisiz kılıyor. Geçmiş arşiv verisi biriktikçe bu düzelecek, ama şu an "
    "AI kararları takip amaçlı, engelleyici değil."
)
pdf.bullet(
    "SMC coin evreni dar. Günlük 5 milyon dolar hacim filtresi 59 coin taramasına yol açıyor. "
    "Daha geniş evren daha fazla fırsat ama daha fazla gürültü de demek."
)
pdf.bullet(
    "T24 devre dışı, yerini dolduracak kısa vadeli strateji yok. bot.py tarafı şu an PANİK "
    "ve iki orta-uzun vade stratejisiyle çalışıyor."
)
pdf.bullet(
    "CHoCH tetiklemesi fiyat konumuna değil, bayrağa bağlı. Coin indirim bölgesine girince "
    "aktif bayrağı kalıcı olarak setleniyor. Fiyat bölgeden uzaklaşsa bile bayrak temizlenmiyor. "
    "Teorik olarak bir coin indirim bölgesinden çok yukarı çıkıp orada CHoCH verse sinyal gidebilir."
)
pdf.bullet(
    "PUMP PROBABILITY için henüz veri yok. Sistem çalışıyor, sinyaller geliyor, ama backtest "
    "ve gerçek sonuç verisi yok. Başarı oranı belirsiz."
)
pdf.divider()

# ── BÖLÜM 9 ────────────────────────────────────────────────────────────────
pdf.chapter_title(9, "Yakın Vadede Beklenenler")
pdf.bullet(
    "PANİK PUMP zengin dataset analizi. Bilgisayarda 150-200 sinyalli eski export var. "
    "Bu dosya yüklendiğinde istatistiksel olarak çok daha anlamlı backtest sonucu elde edilecek."
)
pdf.bullet(
    "AI karar kalitesinin anlamlı hale gelmesi. Yeterli kapanmış sinyal birikince "
    "'AI GİR dedi → WR %X, RİSKLİ dedi → WR %Y' karşılaştırması gerçek anlam taşıyacak."
)
pdf.bullet(
    "PUMP PROBABILITY gerçek performansı netleşecek. Haziran 2026'dan veri toplanıyor. "
    "Birkaç ay içinde BTC filtreli vs filtresiz karşılaştırması yapılabilir."
)
pdf.divider()

# ── BÖLÜM 10 ───────────────────────────────────────────────────────────────
pdf.chapter_title(10, "Uzun Vadeli Sistem Hedefi")
pdf.body(
    "Şu an sistem tamamen reaktif: sinyal gelince çalışır, değerlendirir, sonuç bildirir. "
    "Hedef ise proaktif, hafızalı bir sisteme dönüşmek."
)

pdf.section_title("'Sürekli İzleyen Sistem' Vizyonu")
pdf.bullet("Claude sinyal gelmeden önce piyasayı zaten analiz etmiş olsun")
pdf.bullet("'Bu düşüş gerçek satış baskısından mı, yoksa likidite temizlemesinden mi?' sorusunu önceden cevaplayabilsin")
pdf.bullet("Haber etkisi, büyük analist yorumları, Twitter sentiment — bunlar da bağlama katılsın")
pdf.bullet("Her indikatörü en iyi çalıştığı zaman diliminde kullansın (FBB haftalık, TMA 3 günlük, SSL haftalık)")
pdf.bullet("Tek seferlik değerlendirme değil, sürekli hafıza gerektiriyor")

pdf.section_title("Planlanmış Özellikler")
pdf.kv("GitHub Actions", "Kullanıcı müdahalesi olmadan periyodik analizler — otonom ama insan onaylı")
pdf.kv("İnteraktif Analiz", "Telegram'dan 'btc 1h' veya 'zec 4h' yazınca anında analiz dönmesi")
pdf.kv("Claude Tarama Kanalı", "Bot sinyallerinden bağımsız Claude'un kendi coin taraması yapacağı kanal")
pdf.kv("Durum", "Tüm bu özellikler bekleyen fikir aşamasında — önce mevcut sistemler olgunlaşsın")

pdf.ln(5)
pdf.info_box(
    "Sistem aktif olarak gelişiyor. Temel altyapı oturmuş, veri birikimi başlamış. "
    "Önümüzdeki 3-6 ay kritik — yeterli gerçek veri oluşunca hem AI kararlarının kalibrasyonu "
    "hem de stratejilerin ince ayarı mümkün olacak.",
    color=(235, 245, 255)
)

out = "/home/user/Botum/SİSTEM SUNUM ANLATIMI.pdf"
pdf.output(out)
print(f"PDF olusturuldu: {out}")
