# 코드 학습 목표
# 캘리브레이션
# perchance 동작 제어 


# ------------------------------------------------------------------ #
#  설정                                                                 #
# ------------------------------------------------------------------ #

PERCHANCE_URL = ""

CDN_CONFIG = {
    
}

# Perchance 모바일 Chrome 화면 좌표
COORDS = {
    "generate_btn": (539, 1300),          # share 섹션 있을 때 폴백
    # 현재 화면에 보이는 이미지 중앙
    "image_current": (540, 980),
    # 현재 화면에 보이는 프롬프트 첫번째 커서 위치
    "prompt_first_cursor": (42, 684),
    #키보드 빈곳 클릭으로 내리기
    "keyboard_empty_click": (862, 1207),
    # 프롬프트 첫번째 커서위치에서 더블탭 이후 모두 선택 버튼 위치
    "prompt_all_select": (624, 591),
    # 롱프레스 후 뜨는 메뉴의 '이미지 다운로드' 버튼 좌표
    "download_image_menu": (300, 1300),
    # 다음 이미지로 이동하는 스크롤
    "scroll_next_image": (540, 1120, 540, 280),
}

COORDS_DIR = os.path.join(os.path.dirname(__file__), "coords")


class PerchanceGenerator:
    """
    LaiXi ADB를 사용해 Android Chrome에서 Perchance 이미지를 생성한다.
    MultiDeviceGrokManager와 동일한 인프라(LaixiAdbController)를 공유한다.
    """

    def __init__(self, laixi_controller: LaixiAdbController):
        self.laixi = laixi_controller
        try:
            self.openai_client = OpenAI(
                api_key=''
            )
        except Exception as e:
            Logger.log(f"OpenAI 초기화 오류: {e}")
            self.openai_client = None

    def translate_to_english(self, korean_text: str, custom_system_prompt: str = None) -> str:
        """한국어 → 영어 이미지 프롬프트 변환 (OpenAI GPT-4o, 실패 시 GoogleTranslator)."""
        decoded = korean_text
        try:
            try:
                decoded = unquote(korean_text)
            except Exception:
                pass

            if not self.openai_client:
                return self._fallback_translate(decoded)

            system_content = custom_system_prompt or (
                "다음 한글 설명을 AI 이미지 생성에 적합한 영어 프롬프트로 자연스럽게 변환해줘. "
                "단순 직역하지 말고, 시각적으로 구체화된 영어 프롬프트로 출력해줘.\n"
                "사용자가 단답식으로 입력 했더라도 그 이미지를 만들기 위한 영어 설명을 추가해서 구체화 시켜야함.\n\n"
                "**다른 설명 없이 이미지생성을 위한 영어 프롬프트만 출력**"
            )
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": f"사용자가 입력한 한글 설명: {decoded}"},
                ],
                max_tokens=500,
                temperature=0.3,
            )
            translated = response.choices[0].message.content.strip()
            Logger.log(f"GPT-4o 번역: {translated}")
            return translated
        except Exception as e:
            Logger.log(f"OpenAI 번역 오류: {e}")
            return self._fallback_translate(decoded)

    def _fallback_translate(self, korean_text: str) -> str:
        try:
            translated = GoogleTranslator(source='ko', target='en').translate(korean_text)
            return f"{translated}, detailed, high quality, photorealistic"
        except Exception as e:
            Logger.log(f"대체 번역 실패: {e}")
            return korean_text

    # ------------------------------------------------------------------ #
    #  ADB 헬퍼                                                            #
    # ------------------------------------------------------------------ #

    def _adb(self, device_id: str, command: str):
        """ADB 명령 실행."""
        return self.laixi.execute_adb_command(device_id, command)

    def _tap(self, device_id: str, x: int, y: int):
        self._adb(device_id, f"input tap {x} {y}")

    def _write_clipboard(self, device_id: str, content: str):
        """Laixi writeclipboard 액션으로 클립보드에 텍스트 저장."""
        cmd = json.dumps({
            "action": "writeclipboard",
            "comm": {"deviceIds": device_id, "content": content}
        })
        self.laixi.send_command(cmd)

    def _pull_file(self, device_id: str, phone_path: str, save_path: str):
        """Laixi PullFile 액션으로 기기 파일을 로컬에 저장."""
        cmd = json.dumps({
            "action": "PullFile",
            "comm": {
                "deviceIds": device_id,
                "phoneFilePath": phone_path,
                "savePath": save_path.replace('\\', '/'),
            }
        })
        self.laixi.send_command(cmd)

    def _take_screencap(self, device_id: str):
        """ADB screencap 후 PIL Image 반환. 실패 시 None."""
        try:
            from PIL import Image
            tmp = os.path.join(
                os.path.dirname(__file__),
                f"sc_{device_id.replace(':', '_').replace('.', '_')}.png"
            )
            self._adb(device_id, "screencap -p /sdcard/sc_gen.png")
            time.sleep(0.3)
            self._pull_file(device_id, "/sdcard/sc_gen.png", tmp)
            time.sleep(0.5)
            self._adb(device_id, "rm /sdcard/sc_gen.png")
            if os.path.exists(tmp):
                img = Image.open(tmp).convert("RGB")
                os.remove(tmp)
                return img
        except Exception as e:
            Logger.log(f"[{device_id}] 스크린캡 실패: {e}")
        return None

    def _find_generate_btn(self, device_id: str, img=None):
        """스크린캡으로 generate 버튼 y 좌표 탐지.

        share 섹션 유무로 두 구간을 분리:
          - share 섹션 있음: Art Style → share → generate (y≈1211~1349)
          - share 섹션 없음: Art Style → generate  (y≈1099~1210)
        각 구간에서 dark pixel 밀도가 높은 행을 generate 버튼 텍스트로 판단.
        실패 시 COORDS['generate_btn'] 반환.
        img를 직접 넘기면 screencap을 생략한다 (calibrate_device에서 재사용).
        """
        default = COORDS["generate_btn"]
        try:
            if img is None:
                img = self._take_screencap(device_id)
            if img is None:
                return default

            x_samples = list(range(200, 880, 10))

            def count_dark(y):
                return sum(
                    1 for x in x_samples
                    if 0 <= x < img.width and sum(img.getpixel((x, y))) / 3 < 160
                )

            # 1차 스캔: share 섹션 있는 기기 — generate 버튼이 y=1211~1349
            for y in range(1349, 1210, -1):
                if count_dark(y) >= 8:
                    Logger.log(f"[{device_id}] generate btn 탐지 (share 섹션 있음): y={y}")
                    return 539, y

            # 2차 스캔: share 섹션 없는 기기 — generate 버튼이 y=1099~1210
            for y in range(1210, 1098, -1):
                if count_dark(y) >= 8:
                    Logger.log(f"[{device_id}] generate btn 탐지 (share 섹션 없음): y={y}")
                    return 539, y

            # 폴백: 두 구간 모두 실패 시 넓은 범위 재스캔
            Logger.log(f"[{device_id}] generate btn 좁은 범위 실패, 전체 스캔 시도")
            for y in range(1349, 699, -1):
                if count_dark(y) >= 4:
                    Logger.log(f"[{device_id}] generate btn 탐지 (폴백): y={y}")
                    return 539, y

            Logger.log(f"[{device_id}] generate btn 탐지 실패 → fallback {default}")
            return default
        except Exception as e:
            Logger.log(f"[{device_id}] generate btn 탐지 실패: {e}")
            return default

    def _save_device_coords(self, device_id: str, layout: str, generate_btn: tuple, dark_count: int):
        """캘리브레이션 결과를 기기별 JSON 파일로 저장."""
        os.makedirs(COORDS_DIR, exist_ok=True)
        safe_id = device_id.replace(':', '_').replace('.', '_')
        path = os.path.join(COORDS_DIR, f"perchance_{safe_id}.json")
        data = {
            "device_id": device_id,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "screen": {"width": 1440, "height": 2560},
            "layout": layout,
            "coords": {
                "generate_btn": list(generate_btn),
                "prompt_first_cursor": list(COORDS["prompt_first_cursor"]),
                "prompt_all_select": list(COORDS["prompt_all_select"]),
                "keyboard_empty_click": list(COORDS["keyboard_empty_click"]),
                "image_current": list(COORDS["image_current"]),
                "download_image_menu": list(COORDS["download_image_menu"]),
                "scroll_next_image": list(COORDS["scroll_next_image"]),
            },
            "source": {"generate_btn": "screenshot_scan"},
            "confidence": {"generate_btn": round(min(1.0, dark_count / 40), 2)},
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        Logger.log(f"[{device_id}] 좌표 저장: {path}")

    def _load_device_coords(self, device_id: str) -> Optional[dict]:
        """오늘 날짜의 캘리브레이션 캐시 로드. 없거나 만료됐으면 None."""
        safe_id = device_id.replace(':', '_').replace('.', '_')
        path = os.path.join(COORDS_DIR, f"perchance_{safe_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            today = datetime.now().strftime("%Y-%m-%d")
            if data.get("date") != today:
                Logger.log(f"[{device_id}] 캐시 만료 ({data.get('date')} ≠ {today})")
                return None
            return data
        except Exception as e:
            Logger.log(f"[{device_id}] 캐시 로드 실패: {e}")
            return None

    def _get_generate_btn(self, device_id: str):
        """캐시된 generate_btn 반환. 없으면 live 스캔."""
        cache = self._load_device_coords(device_id)
        if cache:
            coords = cache["coords"]["generate_btn"]
            Logger.log(f"[{device_id}] 캐시 generate_btn: {coords}")
            return tuple(coords)
        Logger.log(f"[{device_id}] 캐시 없음 → live 스캔")
        return self._find_generate_btn(device_id)

    def calibrate_device(self, device_id: str) -> bool:
        """기기별 좌표 캘리브레이션 수행 후 JSON 저장.
        Perchance 페이지를 열고 screencap으로 generate_btn y 좌표를 탐지한다.
        """
        try:
            Logger.log(f"[{device_id}] 캘리브레이션 시작")

            if self._load_device_coords(device_id):
                Logger.log(f"[{device_id}] 오늘 캘리브레이션 이미 완료, 스킵")
                return True

            if not self._open_perchance(device_id):
                return False

            kx, ky = COORDS["keyboard_empty_click"]
            self._tap(device_id, kx, ky)
            time.sleep(1.5)

            img = self._take_screencap(device_id)
            x, y = self._find_generate_btn(device_id, img=img)

            dark_count = 0
            if img:
                x_samples = list(range(200, 880, 10))
                dark_count = sum(
                    1 for sx in x_samples
                    if 0 <= sx < img.width and sum(img.getpixel((sx, y))) / 3 < 160
                )

            layout = "share" if y >= 1211 else "no_share"
            self._save_device_coords(device_id, layout, (x, y), dark_count)

            Logger.log(
                f"[{device_id}] 캘리브레이션 완료: layout={layout}, "
                f"generate_btn=({x},{y}), confidence={min(1.0, dark_count/40):.2f}"
            )
            return True

        except Exception as e:
            Logger.log(f"[{device_id}] 캘리브레이션 실패: {e}")
            traceback.print_exc()
            return False

    # ------------------------------------------------------------------ #
    #  Perchance 자동화 단계                                               #
    # ------------------------------------------------------------------ #

    def _open_perchance(self, device_id: str) -> bool:
        """Android Chrome으로 Perchance URL 오픈."""
        try:
            Logger.log(f"[{device_id}] Perchance 열기: {PERCHANCE_URL}")
            # Chrome에서 URL 오픈
            self._adb(
                device_id,
                f'am start -a android.intent.action.VIEW -d "{PERCHANCE_URL}" com.android.chrome'
            )
            Logger.log(f"[{device_id}] 페이지 로딩 대기 (5초)...")
            time.sleep(5)  # 페이지 + iframe 로딩 대기
        
            Logger.log(f"[{device_id}] Perchance 로딩 완료")
            return True
        except Exception as e:
            Logger.log(f"[{device_id}] Perchance 열기 실패: {e}")
            return False

    def _input_prompt(self, device_id: str, prompt: str) -> bool:
        """Description 필드를 비운 뒤 클립보드로 프롬프트를 붙여넣는다."""
        try:
            Logger.log(f"[{device_id}] 프롬프트 입력 시작: {prompt[:60]}...")

            cx, cy = COORDS["prompt_first_cursor"]
            ax, ay = COORDS["prompt_all_select"]

            Logger.log(f"[{device_id}] 1단계: 포커스 탭 ({cx}, {cy}) — 0.5s 대기")
            self._tap(device_id, cx, cy)
            time.sleep(0.5)

            Logger.log(f"[{device_id}] 2단계: 더블탭 ({cx}, {cy}) — 1.5s 대기")
            self._tap(device_id, cx, cy)
            time.sleep(0.15)
            self._tap(device_id, cx, cy)
            time.sleep(1.5)

            Logger.log(f"[{device_id}] 3단계: 모두 선택 탭 ({ax}, {ay}) — 0.8s 대기")
            self._tap(device_id, ax, ay)
            time.sleep(0.8)

            Logger.log(f"[{device_id}] 4단계: 텍스트 삭제 (KEYCODE_DEL 67) — 0.5s 대기")
            self._adb(device_id, "input keyevent 67")
            time.sleep(0.5)

            Logger.log(f"[{device_id}] 5단계: 클립보드 저장 — 0.5s 대기")
            self._write_clipboard(device_id, prompt)
            time.sleep(0.5)

            Logger.log(f"[{device_id}] 6단계: 붙여넣기 (KEYCODE_PASTE 279) — 1.0s 대기")
            self._adb(device_id, "input keyevent 279")
            time.sleep(1.0)

            Logger.log(f"[{device_id}] 프롬프트 입력 완료")
            return True

        except Exception as e:
            Logger.log(f"[{device_id}] 프롬프트 입력 실패: {e}")
            return False

    def _click_generate(self, device_id: str) -> bool:
        """Generate 버튼 탭."""
        try:
            Logger.log(f"[{device_id}] Generate 버튼 탐색...")

            # 키보드 빈 영역 탭으로 닫기
            kx, ky = COORDS["keyboard_empty_click"]
            self._tap(device_id, kx, ky)
            time.sleep(1)

            x, y = self._get_generate_btn(device_id)
            Logger.log(f"[{device_id}] Generate 탭: ({x}, {y})")
            self._tap(device_id, x, y)
            time.sleep(1)
            Logger.log(f"[{device_id}] Generate 버튼 클릭 완료")
            return True

        except Exception as e:
            Logger.log(f"[{device_id}] Generate 버튼 클릭 실패: {e}")
            return False

    def _cleanup_download_dir(self, device_id: str):
        """Download 폴더의 기존 이미지 파일 삭제 — 새 다운로드와 혼재 방지."""
        dl = "/sdcard/Download"
        self._adb(device_id,
            f"rm -f {dl}/*.jpg {dl}/*.jpeg {dl}/*.png {dl}/*.webp 2>/dev/null; echo done"
        )
        Logger.log(f"[{device_id}] Download 폴더 기존 이미지 정리 완료")

    def _download_images(self, device_id: str, count: int, key: str = None, check_cancelled=None) -> Optional[List[str]]:
        try:
            # 생성 대기 전, 기존 파일 먼저 정리
            self._cleanup_download_dir(device_id)

            Logger.log(f"[{device_id}] 이미지 생성 대기 (45초)...")
            for _ in range(45):
                if check_cancelled and check_cancelled():
                    Logger.log(f"[{device_id}] 작업 취소 감지 - 대기 중단")
                    return []
                time.sleep(1)

            target_count = min(count, 4)
            download_dir = "/sdcard/Download"
            project_root = os.path.dirname(os.path.abspath(__file__))
            local_dir = os.path.join(project_root, "image", device_id)
            os.makedirs(local_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            local_paths = []

            Logger.log(f"[{device_id}] 이미지 다운로드 시작 ({target_count}개)...")

            for i in range(target_count):
                if check_cancelled and check_cancelled():
                    Logger.log(f"[{device_id}] 작업 취소 - 다운로드 중단")
                    break

                # 스크롤
                sx, sy, ex, ey = COORDS["scroll_next_image"]
                Logger.log(f"[{device_id}] 이미지 {i+1} 스크롤")
                self._adb(device_id, f"input swipe {sx} {sy} {ex} {ey} 700")
                time.sleep(2)

                # 롱프레스 → 다운로드 버튼 탭
                x, y = COORDS["image_current"]
                Logger.log(f"[{device_id}] 이미지 {i+1} 롱프레스 ({x}, {y})")
                self._adb(device_id, f"input swipe {x} {y} {x} {y} 1800")
                time.sleep(2)

                dx, dy = COORDS["download_image_menu"]
                Logger.log(f"[{device_id}] 이미지 {i+1} 다운로드 버튼 클릭 ({dx}, {dy})")
                self._tap(device_id, dx, dy)
                time.sleep(5)  # 다운로드 완료 대기

                # 방금 생긴 파일 1개만 가져오기
                list_cmd = (
                    f"ls -t {download_dir}/*.jpg {download_dir}/*.jpeg "
                    f"{download_dir}/*.png {download_dir}/*.webp 2>/dev/null | head -1"
                )
                result = self._adb(device_id, list_cmd)
                new_files = self._parse_file_list(device_id, result, max_count=1)

                if not new_files:
                    Logger.log(f"[{device_id}] 이미지 {i+1} 파일 없음, 건너뜀")
                    continue

                remote_path = new_files[0]
                ext = os.path.splitext(remote_path)[1] or ".jpg"
                local_path = os.path.join(local_dir, f"perchance_{device_id}_{timestamp}_{i+1}{ext}")

                Logger.log(f"[{device_id}] PullFile {i+1}: {remote_path}")
                self._pull_file(device_id, remote_path, local_path)
                time.sleep(3)

                # 즉시 삭제 → 다음 다운로드 시 파일명 충돌 없음
                self._adb(device_id, f"rm '{remote_path}'")

                if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                    Logger.log(f"[{device_id}] 로컬 저장 완료: {local_path}")
                    local_paths.append(local_path)
                else:
                    Logger.log(f"[{device_id}] 파일 저장 실패: {local_path}")

            Logger.log(f"[{device_id}] 이미지 다운로드 완료: {len(local_paths)}/{target_count}개")
            return local_paths if local_paths else None

        except Exception as e:
            Logger.log(f"[{device_id}] 이미지 다운로드 실패: {e}")
            traceback.print_exc()
            return None

    def _parse_file_list(self, device_id: str, result, max_count: int) -> List[str]:
        """ADB ls 결과에서 파일 경로 목록 추출."""
        paths = []
        try:
            if isinstance(result, dict):
                result_data = result.get('result', '')
                if isinstance(result_data, str):
                    try:
                        parsed = json.loads(result_data)
                        if isinstance(parsed, dict) and device_id in parsed:
                            file_list = parsed[device_id]
                            if isinstance(file_list, list):
                                paths = [f.strip() for f in file_list if f and f.strip()]
                    except json.JSONDecodeError:
                        # JSON이 아닌 경우 줄 단위로 파싱
                        paths = [l.strip() for l in result_data.splitlines() if l.strip()]
        except Exception as e:
            Logger.log(f"파일 목록 파싱 오류: {e}")

        return paths[:max_count]

    # ------------------------------------------------------------------ #
    #  CDN 업로드 (multi_device_grok_manager._upload_to_cdn 동일 패턴)     #
    # ------------------------------------------------------------------ #

    def _upload_to_cdn(self, image_path: str) -> Optional[str]:
        """로컬 이미지 파일을 FTP CDN에 업로드하고 URL 반환."""
        try:
            if not os.path.exists(image_path):
                Logger.log(f"[CDN] 파일 없음: {image_path}")
                return None

            timestamp = int(time.time())
            random_id = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))
            ext = os.path.splitext(image_path)[1] or '.jpg'
            remote_filename = f"perchance_{timestamp}_{random_id}{ext}"

            with open(image_path, 'rb') as f:
                image_binary = f.read()

            Logger.log(f"[CDN] FTP 업로드 시작: {remote_filename} ({len(image_binary)} bytes)")

            ftp = None
            try:
                ftp = ftplib.FTP()
                ftp.connect(CDN_CONFIG["ftp_host"], CDN_CONFIG["ftp_port"])
                ftp.login(CDN_CONFIG["ftp_user"], CDN_CONFIG["ftp_pass"])
                if CDN_CONFIG["passive_mode"]:
                    ftp.set_pasv(True)

                upload_path = CDN_CONFIG["ftp_upload_path"].strip('/')
                ftp.cwd('/')
                for folder in upload_path.split('/'):
                    if folder:
                        try:
                            ftp.cwd(folder)
                        except ftplib.error_perm:
                            ftp.mkd(folder)
                            ftp.cwd(folder)

                ftp.storbinary(f'STOR {remote_filename}', BytesIO(image_binary))
                cdn_url = f"{CDN_CONFIG['base_url']}{remote_filename}"
                Logger.log(f"[CDN] 업로드 성공: {cdn_url}")
                return cdn_url

            except (ftplib.all_errors, Exception) as e:
                Logger.log(f"[CDN] FTP 오류: {e}")
                return None
            finally:
                if ftp:
                    try:
                        ftp.quit()
                    except Exception:
                        try:
                            ftp.close()
                        except Exception:
                            pass

        except Exception as e:
            Logger.log(f"[CDN] 업로드 중 오류: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    def generate_with_params(
        self,
        device_id: str,
        prompt: str,
        count: int = 2,
        shape: str = "portrait",
        key: str = None,
        check_cancelled=None,
    ) -> Dict:
        """
        Android 기기에서 Perchance 이미지를 생성하고 CDN URL 목록을 반환한다.

        반환 구조 (MultiDeviceGrokManager._send_callback 페이로드와 호환):
          {status, message, images, translated_prompt, metadata}
        """
        try:
            if not (1 <= count <= 4):
                return {"status": "failed",
                        "error": f"count는 1-4 사이여야 합니다. 요청된 값: {count}",
                        "images": [], "translated_prompt": None}

            # 취소 확인 1
            if check_cancelled and check_cancelled():
                return {"status": "cancelled", "error": "작업이 취소되었습니다",
                        "images": [], "translated_prompt": None}

            Logger.log(f"[{device_id}] 수신 프롬프트: {prompt}")

            # 1. Perchance 열기
            if not self._open_perchance(device_id):
                return {"status": "failed", "error": "Perchance 열기 실패",
                        "images": [], "translated_prompt": prompt}

            # 취소 확인 2
            if check_cancelled and check_cancelled():
                return {"status": "cancelled", "error": "작업이 취소되었습니다",
                        "images": [], "translated_prompt": prompt}

            # 2. 프롬프트 입력
            if not self._input_prompt(device_id, prompt):
                return {"status": "failed", "error": "프롬프트 입력 실패",
                        "images": [], "translated_prompt": prompt}

            # 취소 확인 3
            if check_cancelled and check_cancelled():
                return {"status": "cancelled", "error": "작업이 취소되었습니다",
                        "images": [], "translated_prompt": prompt}

            # 3. Generate 버튼
            if not self._click_generate(device_id):
                return {"status": "failed", "error": "Generate 버튼 클릭 실패",
                        "images": [], "translated_prompt": prompt}

            # 취소 확인 4
            if check_cancelled and check_cancelled():
                return {"status": "cancelled", "error": "작업이 취소되었습니다",
                        "images": [], "translated_prompt": prompt}

            # 4. 이미지 다운로드
            local_paths = self._download_images(device_id, count, key, check_cancelled=check_cancelled)
            if not local_paths:
                # 취소된 경우 재시도 없이 즉시 반환
                if check_cancelled and check_cancelled():
                    return {"status": "cancelled", "error": "작업이 취소되었습니다",
                            "images": [], "translated_prompt": prompt}
                # 좌표 캐시 문제일 수 있으므로 무효화 후 재캘리브레이션 + 1회 재시도
                Logger.log(f"[{device_id}] 다운로드 실패 → 재캘리브레이션 후 1회 재시도")
                safe_id = device_id.replace(':', '_').replace('.', '_')
                cache_path = os.path.join(COORDS_DIR, f"perchance_{safe_id}.json")
                if os.path.exists(cache_path):
                    os.remove(cache_path)
                self.calibrate_device(device_id)

                if not self._click_generate(device_id):
                    return {"status": "failed", "error": "재시도 Generate 버튼 클릭 실패",
                            "images": [], "translated_prompt": prompt}

                local_paths = self._download_images(device_id, count, key, check_cancelled=check_cancelled)
                if not local_paths:
                    return {"status": "failed", "error": "이미지 다운로드 실패 (재시도 포함)",
                            "images": [], "translated_prompt": prompt}

            # 취소 확인 5
            if check_cancelled and check_cancelled():
                # 로컬 파일 정리
                for p in local_paths:
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except Exception:
                        pass
                return {"status": "cancelled", "error": "작업이 취소되었습니다",
                        "images": [], "translated_prompt": prompt}

            # 5. CDN 업로드
            uploaded_images = []
            for i, local_path in enumerate(local_paths, 1):
                cdn_url = self._upload_to_cdn(local_path)
                try:
                    os.remove(local_path)
                except Exception:
                    pass

                if cdn_url:
                    uploaded_images.append({
                        "id": f"img_{i}",
                        "url": cdn_url,
                        "filename": os.path.basename(local_path),
                        "method": "FTP",
                    })
                    Logger.log(f"[{device_id}] 이미지 {i} 업로드 완료: {cdn_url}")
                else:
                    Logger.log(f"[{device_id}] 이미지 {i} 업로드 실패")

            Logger.log(f"[{device_id}] 완료: {len(uploaded_images)}/{count}개")

            return {
                "status": "completed",
                "message": f"{len(uploaded_images)}개 이미지 생성 완료",
                "images": uploaded_images,
                "translated_prompt": prompt,
                "metadata": {
                    "prompt": prompt,
                    "shape": shape,
                    "style": "perchance",
                    "requested_count": count,
                    "generated_count": len(uploaded_images),
                },
            }

        except Exception as e:
            Logger.log(f"[{device_id}] 생성 오류: {e}")
            traceback.print_exc()
            return {"status": "failed", "error": str(e),
                    "images": [], "translated_prompt": None}
