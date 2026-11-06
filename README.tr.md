# Gemini Bridge

[English](README.md) · [Sürümler / indir](https://github.com/ipekbayrak/gemini-bridge/releases)

**Codex planlar ve tasarlar; yerel Antigravity rutin işleri yürütür.**

Bu MIT lisanslı eklenti, yerel eklenti çalıştırabilen ChatGPT/Codex masaüstü kodlama sohbetinden, bilgisayarınızdaki resmî `agy` CLI'ye görev verir. Her kullanıcı **kendi Antigravity hesabıyla** giriş yapar. Kimlik bilgisi, OAuth token'ı veya ortak hesap dağıtılmaz.

## İş bölümü

| Codex'te kalır | Gemini'ye devredilir |
|---|---|
| Genel planlama, mimari, derin analiz | Onaylanmış yapının rutin uygulanması |
| UI ana kabuğu ve etkileşim tasarımı | Bilinen kod kalıpları, mekanik düzenlemeler |
| İkon/logo ve önemli görseller | Dosya tarama, kaynak toplama, özetleme |
| Önemli kararlar ve son kabul | Uyumlu test/build işleri, Chrome ve eklenti testleri |

Amaç OpenAI tarafındaki uygulama yükünü azaltmaktır. **Kota sıfırlanmaz veya aşılmaz:** planlama, orkestrasyon ve inceleme OpenAI kotasını; devredilen işler Antigravity kotasını kullanır. Tasarruf oranı işin türüne göre değişir.

## Gereksinimler

- macOS veya Linux; Windows bu sürümde desteklenmiyor.
- Python 3.11+, Codex masaüstü/CLI ve resmî Antigravity CLI 1.5.1+.
- `agy` komutunu bir kez etkileşimli açıp kendi hesabınıza giriş yapın.
- Hesabınızda `gemini-4-argon-high` ve `gemini-4-argon-medium` modelleri bulunmalı; kurucu kontrol eder. Başka modele sessiz geçiş yoktur.
- Tarayıcı işleri için Google Chrome ve npm ile Node 20.x ≥20.19, 22.x ≥22.12 veya 23+.

**ChatGPT web ve mobil sohbetlerinde doğrudan çalışmaz.** Bilgisayarda yerel kod ve eklenti çalıştırabilen masaüstü sohbeti gerekir.

## Kurulum

```sh
git clone https://github.com/ipekbayrak/gemini-bridge.git
cd gemini-bridge
python3 scripts/setup.py check --with-chrome --json
python3 scripts/setup.py install --with-chrome --auto-route --allow-development --disable-credit-overage
```

Seçenekler:

- `--with-chrome`: İzole Chrome DevTools sunucusunu, eklenti test araçlarını ve ona ait dar kapsamlı izinleri yapılandırır.
- `--auto-route`: Genel Codex talimatlarına yalnız işaretli bir bölüm ekler; yeni sohbetlerde otomatik iş bölümü sağlar.
- `--allow-development`: Belirli geliştirme/test komutları ile resmî Modern Web Guidance yardımcısına izin verir. Tüm komutlara sınırsız izin vermez.
- `--disable-credit-overage`: Antigravity'nin ek ücretli AI kredisine geçişini kapatır.

Yalnız `python3 scripts/setup.py install` çalıştırırsanız bu isteğe bağlı ayarlar değiştirilmez. Mevcut `chrome-devtools` sunucusu korunur; özellikle değiştirmek istiyorsanız `--with-chrome --replace-chrome` kullanın.

Kurucu paketi Codex klasöründe kalıcı bir konuma kopyalar. İndirilen ZIP klasörü sonradan silinse de kurulum çalışır. Önceki ayarlar ve başka eklentiler korunur.

## Kullanım

Masaüstü yazma alanında `/gemini-bridge` yazıp çıkan skill'i seçin. `$gemini-bridge` veya CLI'de `/skills` de kullanılabilir. Menü güncellenmediyse yeni sohbet açın veya uygulamayı yeniden açın.

```text
/gemini-bridge Bu projeyi tamamla. Mimari ve UI sende;
onayladığın tasarımı uygulama ve test işlerini Gemini'ye devret.
```

Otomatik iş bölümü açıksa eklentinin adını yazmanız gerekmez. Varsayılan Heavy, CLI'de `gemini-4-argon-high`; kısa ve net mekanik işler için Medium, `gemini-4-argon-medium` kullanılır.

## Gerçek sınırlar

Chrome ayrı, headless ve izole profil kullanır; normal Chrome hesabınızdaki oturumlara otomatik erişmez. Yerel komutlar sandbox içinde otomatik çalışır; açık güvenlik retleri aşılmaz.

**Gradle/Xcode için koşulsuz destek yoktur.** Bazı Gradle daemon soketleri ve Xcode sistem servisleri sandbox içinde erişilemeyebilir. Python ve tarayıcı testlerinin geçmesi Android/iOS derlemesinin de geçeceği anlamına gelmez. Araç hatası veya güvenlik reddi `needs_review` sonucuna dönüştürülür; tamamlanmamış kontrol başarılı sayılmaz. Reddedilen işlem başka terminale taşınarak otomatik olarak tekrar denenmez.

## Gizlilik, test ve kaldırma

Görev metni ve Antigravity'nin okuduğu kod, kullanıcının sağlayıcı hesabı üzerinden işlenir. Köprünün kendi bulut sunucusu veya analitiği yoktur. Yerel iş günlükleri kod ve görev verisi içerebilir; başkalarıyla paylaşmayın. Hesap anahtarları pakete kopyalanmaz.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/release_check.py
```

Birim ve paket testleri sahte CLI ve geçici dizinler kullanır; model kotası tüketmez. macOS üzerinde gerçek Antigravity/Chrome akışı ayrıca test edilmiştir. Linux CI, hesaba giriş yapılmış uçtan uca test anlamına gelmez.

```sh
codex plugin remove antigravity-bridge@gemini-bridge-local
```

Kaldırma görev günlüklerini veya isteğe bağlı eklenen ayarları silmez. Otomatik iş bölümünü kapatmak için genel `AGENTS.md` içindeki yalnız Gemini Bridge işaretli bloğu kaldırın.

MIT lisansı: [LICENSE](LICENSE). Google veya OpenAI'nin resmî ürünü değildir. GitHub'da açık kaynak paylaşımı, OpenAI eklenti mağazasında onaylı yayın anlamına gelmez.

## Göreve özel salt okunur erişim (0.3.0)

Orkestratör, insanın görevde önceden yetkilendirdiği adresleri/dosyaları iş başlamadan `agy_start.access` alanına koyar. CLI karşılığı `--access-file erişim.json` seçeneğidir:

```json
{"read_urls":["https://example.invalid/sayfa"],"read_files":["/tam/onaylanmis-dosya.json"]}
```

Köprü yalnız o oturum için alan adı/dosya okuma izinleri ekler. Genel ayarlara, yazma/etkileşim izinlerine dokunmaz. Mevcut `deny/ask` ile çatışırsa işi başlatmadan bildirir; reddedilen eylemi otomatik yeniden denemez. Sağlayıcının alan adı izni alt alanları kapsayabilir; tam URL sınırı işletim sistemi duvarı değil, görev sözleşmesidir.

“Dosyada yalnız genel anahtar var” ifadesi dosyayı kendiliğinden okumaya veya içeriğini başka siteye göndermeye izin vermez. Yeni kapsam gerçek insan yetkisine dayanmalıdır. Engelde `blocked_access` ve `next_action` somut olarak incelenir; çalışanın genel onay sorusu kullanıcıya aynen aktarılmaz.

## Açık sohbetler ve sürüm geçişi

Kurucu `~/.local/bin/gemini-bridge` komutunu ekler. Her çağrı güncel etkin kurulumu doğrulayıp kullanır; `--access-file` dahil argümanlar aynen korunur. Açık sohbetlerde yeni işler bu girişe otomatik yönlendirilir; aynı görevdeki mevcut onay sırf sürüm geçişi nedeniyle yeniden sorulmaz. Kapsam genişletilmez ve reddedilmiş iş otomatik tekrar denenmez.

```sh
~/.local/bin/gemini-bridge --where
```

Yeni MCP bağlantıları her istekte güncel kodu seçer. Önceden açılmış eski bir MCP sürecinin belleği dosya kopyalayarak değişmez; masaüstünün canlı yenileme bağlantısı yoksa sohbeti kapatmadan güncel CLI kullanılır. Bu akış eski taşıma bağlantılarının fiziksel olarak yenilendiği iddiası değildir. Devam eden ayrık işler aynı durum dizininde korunur.

## Doğrulama ve kurulum hataları

Başka bir sohbette önceden yetkilendirilmiş gerçek site/dosya denetimi, sahte verili birim testlerinden ayrı doğrulanır. Motor sürümü, korunmuş kapsam ve ret sayısı kaydedilir; müşteri dosyaları/ham günlükler public pakete konulmaz. Dışarıdaki dosyaya salt okunur erişim ile görevde açıkça istenmiş çalışma dizini içi kanıt yazımı çelişmez.

Kurulum, mevcut sabit komutla çatışmayı dosya kopyalama/kayıt işleminden önce kontrol eder. Kaynak değişimi başarısızsa eski çalışan sürüm geri yüklenir. Daha geç bir adım hatasında `partial`, `completed_steps` ve eklenti kaydının gerçekleşip gerçekleşmediği doğru bildirilir.
