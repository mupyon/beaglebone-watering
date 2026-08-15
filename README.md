# BeagleBone Watering Controller

GPIO sysfs と Open-Meteo を使う、Python 3 製の複数ライン散水コントローラです。外部 Python パッケージは使いません。

| ライン | 用途 | GPIO |
| --- | --- | ---: |
| `line1` | 花壇ライン | 48 |
| `line2` | 植木ライン | 49 |
| `line3` | スプリンクラー | 115 |

## セットアップ

BeagleBone 上で root 権限を使い、ファイルを `/opt/watering` に配置します。

```sh
sudo mkdir -p /opt/watering
sudo cp watering.py config.example.json /opt/watering/
sudo cp /opt/watering/config.example.json /opt/watering/config.json
sudo chmod 755 /opt/watering/watering.py
sudo editor /opt/watering/config.json
```

`/sys/class/gpio` への書き込み権限が必要です。通常は root の cron から実行します。対象カーネルで GPIO sysfs が有効であり、指定 GPIO がピン mux 済みで、リレー回路が BeagleBone の電気仕様に適合していることを確認してください。起動時と異常時には次で全バルブを OFF にできます。

```sh
sudo /opt/watering/watering.py off
```

## 設定

`config.example.json` を `config.json` にコピーして編集します。各ラインの `label` は `status` に表示する用途名です。各ラインは独立して次を持ちます。

- `water_balance`: 初回の推定水分量 (mm)。以後の値は `state.json` に保存されます。
- `watering_threshold`: この値以下で自動散水する閾値 (mm)。
- `et0_crop_factor`: ET0 に掛ける作物係数。
- `rain_efficiency`: 降水量のうち土壌水分として有効な比率。
- `default_watering_seconds`: `start` / `auto` の既定散水秒数。
- `watering_calibration`: 1 秒の散水で補充される水量 (mm/秒)。

グローバル設定の `skip_rain_mm` または `skip_probability_percent` のどちらかに今後 `forecast_hours`（既定 12 時間）の予報が達すると、`auto` は散水を見送ります。`active_high` はリレー論理、`gpio_root` は GPIO sysfs の場所です。

`allow_simultaneous` は既定で `false` です。この場合、手動 `start` も同時散水を拒否し、`auto` は閾値以下のうち最も乾いた 1 ラインだけを開始します。

状態更新では、前回取得後（初回は過去 24 時間）の時間別データから、各ラインに次を適用します。

```text
water_balance += precipitation * rain_efficiency - ET0 * et0_crop_factor
water_balance += actual_watering_seconds * watering_calibration
```

## コマンド

```sh
sudo /opt/watering/watering.py status
sudo /opt/watering/watering.py weather
sudo /opt/watering/watering.py start line1
sudo /opt/watering/watering.py start line2 120
sudo /opt/watering/watering.py stop line2
sudo /opt/watering/watering.py off
sudo /opt/watering/watering.py auto
```

`start` はバックグラウンドで開始し、PID を `watering-LINE.pid` に保存します。`stop` は実際に開いていた時間分だけ水分量へ反映します。運用データは `/opt/watering/state.json` と `/opt/watering/watering.log` に保存されます。

## cron 例

Open-Meteo の更新と自動判定を 1 時間ごとに行い、再起動時にはまず全ラインを OFF にします。

```cron
@reboot /opt/watering/watering.py off >>/opt/watering/cron.log 2>&1
7 * * * * /opt/watering/watering.py auto >>/opt/watering/cron.log 2>&1
```

同時散水禁止時、複数ラインが閾値以下なら 1 回の `auto` で 1 ラインだけ開始します。既定散水時間が 1 時間未満なら、次回 cron で次のラインが選ばれます。

## テスト

構文チェック（ネットワーク、GPIO アクセスなし）:

```sh
python3 -m py_compile watering.py
```

実機 GPIO を操作せず設定・状態表示・バックグラウンド開始/停止を確認できます。テスト用ディレクトリで行うため、運用状態には触れません。

```sh
tmpdir="$(mktemp -d)"
cp watering.py config.example.json "$tmpdir/"
cp "$tmpdir/config.example.json" "$tmpdir/config.json"
python3 "$tmpdir/watering.py" --config "$tmpdir/config.json" --dry-run status
python3 "$tmpdir/watering.py" --config "$tmpdir/config.json" --dry-run start line1 2
sleep 3
python3 "$tmpdir/watering.py" --config "$tmpdir/config.json" --dry-run status
```

`weather` と `auto` は Open-Meteo へのネットワーク接続を行います。実機ではまず `--dry-run weather`、次に `--dry-run auto` で予報値と判定を確認してください。
