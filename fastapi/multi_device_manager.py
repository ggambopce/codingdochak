class MultiDeviceGrokManager:
    """20대 폰보드를 동시에 관리하여 Grok 이미지 생성을 병렬 처리하는 매니저"""
    
    # 테스트용 디바이스 화이트리스트 (None이면 모든 디바이스 사용)
    TEST_DEVICE_WHITELIST = None  # 모든 디바이스 사용
    
    # 디바이스 블랙리스트 (제외할 디바이스 목록)
    DEVICE_BLACKLIST = ["R38N106MTKK"]  # 비활성화할 디바이스
    
    def __init__(self, laixi_controller: LaixiAdbController, cdn_callback_url: str, max_concurrent_devices: int = 20):
        self.laixi_controller = laixi_controller
        self.cdn_callback_url = cdn_callback_url
        self.max_concurrent_devices = max_concurrent_devices
        self.SCREEN_WIDTH = 1080
        self.SCREEN_HEIGHT = 1920
        self.device_status = {}  # 디바이스별 상태 관리
        # 동시 요청 시 동일 디바이스 중복 할당을 막기 위한 락
        import threading
        self._allocation_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=max_concurrent_devices)
        self.cancelled_keys = set()  # 취소된 작업 key 추적
        self._last_allocated_index = -1  # 라운드 로빈을 위한 마지막 할당 인덱스
        
    def initialize_devices(self) -> List[str]:
        """연결된 모든 디바이스 목록을 가져오고 초기화 (동기 방식)"""
        try:
            Logger.log("디바이스 목록 조회 중...")
            
            # 동기 방식으로 디바이스 목록 조회
            devices = self.laixi_controller.get_devices()
            
            if not devices:
                Logger.log("연결된 디바이스가 없습니다.")
                return []
            
            Logger.log(f"디바이스 데이터: {devices}")
            
            # 기존 MultiChrome과 동일한 방식으로 디바이스 ID 추출
            device_ids = []
            for device in devices:
                # deviceId가 있으면 사용, 없으면 name 사용
                device_id = device.get('deviceId') or device.get('name', '')
                if device_id:
                    # 블랙리스트 필터링 (우선 적용)
                    if device_id in self.DEVICE_BLACKLIST:
                        Logger.log(f"디바이스 제외 (블랙리스트): {device_id}")
                        continue
                    
                    # 테스트 화이트리스트 필터링
                    if self.TEST_DEVICE_WHITELIST is not None:
                        if device_id in self.TEST_DEVICE_WHITELIST:
                            device_ids.append(device_id)
                            Logger.log(f"디바이스 발견 (테스트용): {device_id} (deviceId: {device.get('deviceId')}, name: {device.get('name')})")
                        else:
                            Logger.log(f"디바이스 제외 (테스트 화이트리스트 아님): {device_id}")
                    else:
                        device_ids.append(device_id)
                        Logger.log(f"디바이스 발견: {device_id} (deviceId: {device.get('deviceId')}, name: {device.get('name')})")
            
            if self.TEST_DEVICE_WHITELIST is not None:
                Logger.log(f"테스트 모드: {len(device_ids)}개 디바이스만 사용 (화이트리스트: {self.TEST_DEVICE_WHITELIST})")
            else:
                Logger.log(f"총 {len(device_ids)}개 디바이스 발견: {device_ids}")
            
            # 디바이스 상태 초기화
            for device_id in device_ids:
                self.device_status[device_id] = {
                    'status': 'idle',
                    'current_task': None,
                    'last_activity': None,
                    'error_count': 0
                }
                
            return device_ids
            
        except Exception as e:
            Logger.log(f"디바이스 초기화 실패: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def generate_images_parallel(self, prompts: List[str], selected_devices: List[str] = None) -> Dict[str, str]:
        """
        여러 디바이스를 동시에 사용하여 이미지를 병렬 생성 (동기 방식)
        
        Args:
            prompts: 생성할 이미지의 프롬프트 목록
            selected_devices: 사용할 디바이스 목록 (None이면 모든 디바이스 사용)
            
        Returns:
            Dict[device_id, cdn_url]: 디바이스별 생성된 이미지 CDN URL
        """
        try:
            # 사용 가능한 디바이스 확인
            available_devices = self._get_available_devices(selected_devices)
            if not available_devices:
                Logger.log("사용 가능한 디바이스가 없습니다.")
                return {}
                
            Logger.log(f"병렬 이미지 생성 시작: {len(prompts)}개 프롬프트, {len(available_devices)}개 디바이스")
            
            # 프롬프트와 디바이스를 매칭
            device_prompt_pairs = self._match_devices_to_prompts(available_devices, prompts)
            
            # 동기 방식으로 순차 실행 (안정성을 위해)
            results = {}
            for device_id, prompt in device_prompt_pairs:
                try:
                    cdn_url = self._generate_single_image_with_device(device_id, prompt)
                    if cdn_url:
                        results[device_id] = cdn_url
                        Logger.log(f"[{device_id}] 이미지 생성 완료: {cdn_url}")
                    else:
                        Logger.log(f"[{device_id}] 이미지 생성 실패")
                except Exception as e:
                    Logger.log(f"[{device_id}] 이미지 생성 중 오류: {e}")
                        
            Logger.log(f"병렬 이미지 생성 완료: {len(results)}개 성공")
            return results
            
        except Exception as e:
            Logger.log(f"병렬 이미지 생성 실패: {e}")
            traceback.print_exc()
            return {}
    
    def _get_available_devices(self, selected_devices: List[str] = None) -> List[str]:
        """사용 가능한 디바이스 목록 반환 (동기 방식)"""
        if selected_devices:
            # 선택된 디바이스 중 사용 가능한 것만 필터링
            return [device_id for device_id in selected_devices 
                   if self.device_status.get(device_id, {}).get('status') == 'idle']
        else:
            # 모든 유휴 디바이스 반환
            return [device_id for device_id, status in self.device_status.items() 
                   if status.get('status') == 'idle']
    
    def _match_devices_to_prompts(self, devices: List[str], prompts: List[str]) -> List[Tuple[str, str]]:
        """디바이스와 프롬프트를 매칭"""
        pairs = []
        for i, prompt in enumerate(prompts):
            device_id = devices[i % len(devices)]  # 라운드 로빈 방식으로 디바이스 할당
            pairs.append((device_id, prompt))
        return pairs
    
    def _check_cancelled(self, key: str) -> bool:
        """작업이 취소되었는지 확인"""
        return key in self.cancelled_keys
    
    def mark_as_cancelled(self, key: str):
        """작업을 취소로 표시"""
        self.cancelled_keys.add(key)
        Logger.log(f"[KEY={key}] 작업이 취소로 표시되었습니다")
    
    def remove_cancelled(self, key: str):
        """취소 플래그 제거"""
        self.cancelled_keys.discard(key)
    
    def _generate_single_image_with_device(self, device_id: str, prompt: str, key: str = None, uniq: str = None, callback_url: str = None) -> Optional[str]:
        """단일 디바이스에서 이미지 생성 (동기 방식)"""
        try:
            # 디바이스 상태를 'busy'로 변경
            self.device_status[device_id]['status'] = 'busy'
            self.device_status[device_id]['current_task'] = prompt
            self.device_status[device_id]['last_activity'] = datetime.now()
            
            Logger.log(f"[{device_id}] 이미지 생성 시작 (Grok 이미 실행됨): {prompt}")
            
            # 취소 체크
            if key and self._check_cancelled(key):
                Logger.log(f"[{device_id}] 작업이 취소되었습니다 (KEY={key})")
                raise Exception("작업이 취소되었습니다")
            
            # 프롬프트 입력 (Grok은 이미 열려있음)
            success = self._input_prompt(device_id, prompt)
            if not success:
                raise Exception("프롬프트 입력 실패")
            
            # 취소 체크
            if key and self._check_cancelled(key):
                Logger.log(f"[{device_id}] 작업이 취소되었습니다 (KEY={key}, 프롬프트 입력 후)")
                raise Exception("작업이 취소되었습니다")
            
            # 이미지 생성 버튼 클릭
            success = self._click_generate_button(device_id)
            if not success:
                raise Exception("생성 버튼 클릭 실패")
            
            # 취소 체크 (이미지 생성 대기 전)
            if key and self._check_cancelled(key):
                Logger.log(f"[{device_id}] 작업이 취소되었습니다 (KEY={key}, 이미지 생성 대기 전)")
                raise Exception("작업이 취소되었습니다")
            
            # 이미지 다운로드 (3개)
            local_image_paths = self._download_images(device_id, key)
            if not local_image_paths:
                raise Exception("이미지 다운로드 실패")
            
            # 취소 체크 (이미지 다운로드 후)
            if key and self._check_cancelled(key):
                Logger.log(f"[{device_id}] 작업이 취소되었습니다 (KEY={key}, 이미지 다운로드 후)")
                raise Exception("작업이 취소되었습니다")
            
            # 3개 이미지 모두 CDN 업로드
            cdn_urls = []
            for idx, local_image_path in enumerate(local_image_paths):
                Logger.log(f"[{device_id}] 이미지 {idx+1}/3 CDN 업로드 중...")
                cdn_url = self._upload_to_cdn(local_image_path)
                if cdn_url:
                    cdn_urls.append(cdn_url)
                    Logger.log(f"[{device_id}] 이미지 {idx+1}/3 CDN 업로드 완료: {cdn_url}")
                else:
                    Logger.log(f"[{device_id}] 이미지 {idx+1}/3 CDN 업로드 실패")
            
            if not cdn_urls:
                raise Exception("CDN 업로드 실패 (모든 이미지)")
            
            # 로컬 이미지 삭제
            Logger.log(f"[{device_id}] 로컬 이미지 삭제 시작...")
            for local_image_path in local_image_paths:
                try:
                    if os.path.exists(local_image_path):
                        os.remove(local_image_path)
                        Logger.log(f"[{device_id}] 로컬 이미지 삭제 완료: {local_image_path}")
                except Exception as delete_error:
                    Logger.log(f"[{device_id}] 로컬 이미지 삭제 실패: {delete_error}")
            
            # 콜백 전송 (CDN URL 리스트 전송)
            self._send_callback(device_id, prompt, cdn_urls, key, uniq, callback_url)
            
            Logger.log(f"[{device_id}] 이미지 생성 성공: {len(cdn_urls)}개 이미지 업로드 완료")
            return cdn_urls[0] if cdn_urls else None
            
        except Exception as e:
            Logger.log(f"[{device_id}] 이미지 생성 실패: {e}")
            self.device_status[device_id]['error_count'] += 1
            
            # 실패 콜백 전송
            if key and uniq:
                self._send_failure_callback(device_id, prompt, str(e), key, uniq, callback_url)
            
            return None
            
        finally:
            # 디바이스 상태를 'idle'로 복원
            self.device_status[device_id]['status'] = 'idle'
            self.device_status[device_id]['current_task'] = None
    
    def _send_failure_callback(self, device_id: str, prompt: str, error_msg: str, key: str, uniq: str, callback_url: str = None):
        """실패 콜백 전송 (exmaple2.py 형식 참고)"""
        try:
            import requests
            
            # 콜백 URL 결정 및 유효성 검사
            target_url = callback_url or self.cdn_callback_url
            
            # URL 유효성 검사
            if not target_url or not (target_url.startswith('http://') or target_url.startswith('https://')):
                Logger.log(f"[{device_id}] ERROR: 유효하지 않은 콜백 URL: {target_url}")
                Logger.log(f"  - callback_url 파라미터: {callback_url}")
                Logger.log(f"  - self.cdn_callback_url: {self.cdn_callback_url}")
                Logger.log(f"  - 기본 콜백 URL 사용: {self.cdn_callback_url}")
                target_url = self.cdn_callback_url
            
            Logger.log(f"[{device_id}] 실패 콜백 전송 준비:")
            Logger.log(f"  - callback_url 파라미터: {callback_url}")
            Logger.log(f"  - self.cdn_callback_url: {self.cdn_callback_url}")
            Logger.log(f"  - 최종 target_url: {target_url}")
            
            # 콜백 페이로드 구성 (exmaple2.py 형식)
            callback_payload = {
                "key": key,
                "uniq": uniq,
                "callback": target_url,
                "state": "error",
                "data": {
                    "translation": {
                        "original": prompt,
                        "translated": None
                    }
                },
                "error": {
                    "code": "GENERATION_FAILED",
                    "message": error_msg,
                    "details": f"Device: {device_id}"
                },
                "message": "이미지 생성이 실패했습니다.",
                "processing_time_seconds": 0
            }
            
            # Form 데이터 준비
            json_data = json.dumps(callback_payload, ensure_ascii=False)
            form_data = {
                'key': key,
                'uniq': uniq,
                'json': json_data
            }
            
            Logger.log(f"[{device_id}] 실패 콜백 전송: {target_url}")
            Logger.log(f"[{device_id}] 실패 콜백 데이터: key={key}, uniq={uniq}, error={error_msg}")
            
            response = requests.post(
                target_url,
                data=form_data,
                timeout=10
            )
            
            if response.status_code == 200:
                Logger.log(f"[{device_id}] 실패 콜백 전송 성공")
            else:
                Logger.log(f"[{device_id}] 실패 콜백 전송 실패 (HTTP {response.status_code})")
                
        except Exception as e:
            Logger.log(f"[{device_id}] 실패 콜백 전송 중 오류: {e}")
    
    def _launch_grok_app(self, device_id: str) -> bool:
        """Grok 앱(ai.x.grok) 실행 - ADB를 사용하여 앱을 포그라운드로 실행"""
        try:
            Logger.log(f"[{device_id}] Grok 앱 실행 시작...")
            
            # 현재 실행 중인 앱 확인 (앱이 이미 실행 중일 수 있음)
            # ADB shell은 Linux 환경이므로 grep 사용 가능
            current_app_cmd = "dumpsys activity activities | grep mResumedActivity"
            try:
                result = self.laixi_controller.execute_adb_command(device_id, current_app_cmd)
                
                # Grok 앱이 이미 실행 중인지 확인
                if result and isinstance(result, dict):
                    result_str = str(result.get('result', ''))
                    if 'ai.x.grok' in result_str:
                        Logger.log(f"[{device_id}] Grok 앱이 이미 포그라운드에서 실행 중입니다")
                        return True
            except Exception as check_error:
                Logger.log(f"[{device_id}] 앱 실행 상태 확인 실패 (계속 진행): {check_error}")
            
            # 앱이 실행 중이 아니면 실행
            Logger.log(f"[{device_id}] Grok 앱 실행 중...")
            # monkey 명령으로 앱 실행 (런처 액티비티 자동 탐지)
            # monkey는 패키지명만으로 앱을 실행할 수 있어 가장 안전한 방법
            self.laixi_controller.execute_adb_command(device_id, "monkey -p ai.x.grok -c android.intent.category.LAUNCHER 1")
            time.sleep(2)  # 앱 초기 로딩 대기
            
            # 앱 실행 직후 강제로 세로 모드 고정 (앱이 가로 모드를 강제하는 경우 대응)
            try:
                Logger.log(f"[{device_id}] 앱 실행 후 세로 모드 강제 고정 중...")
                # content insert로 강제 고정 (더 강력한 방법)
                self.laixi_controller.execute_adb_command(device_id, "content insert --uri content://settings/system --bind name:s:accelerometer_rotation --bind value:i:0")
                time.sleep(0.3)
                self.laixi_controller.execute_adb_command(device_id, "content insert --uri content://settings/system --bind name:s:user_rotation --bind value:i:0")
                time.sleep(0.3)
                # wm 명령으로 이중 확인
                self.laixi_controller.execute_adb_command(device_id, "wm set-user-rotation lock 0")
                time.sleep(0.5)
                Logger.log(f"[{device_id}] 세로 모드 강제 고정 완료")
            except Exception as force_rotation_error:
                Logger.log(f"[{device_id}] 세로 모드 강제 고정 실패 (계속 진행): {force_rotation_error}")
            
            time.sleep(1)  # 추가 안정화 대기
            
            # 앱이 정상적으로 실행되었는지 확인 (선택적)
            try:
                result = self.laixi_controller.execute_adb_command(device_id, current_app_cmd)
                if result and isinstance(result, dict):
                    result_str = str(result.get('result', ''))
                    if 'ai.x.grok' in result_str:
                        Logger.log(f"[{device_id}] Grok 앱 실행 완료 (포그라운드 확인됨)")
                    else:
                        Logger.log(f"[{device_id}] Grok 앱 실행 완료 (확인 불가, 계속 진행)")
            except Exception:
                Logger.log(f"[{device_id}] Grok 앱 실행 완료 (상태 확인 생략)")
            
            return True
            
        except Exception as e:
            Logger.log(f"[{device_id}] Grok 앱 실행 실패: {e}")
            # 실패해도 계속 진행 (앱이 이미 실행 중일 수 있음)
            return True
    
    def _input_prompt(self, device_id: str, prompt: str) -> bool:
        """프롬프트 입력 (좌표: 410, 2050)"""
        try:
            Logger.log(f"[{device_id}] 프롬프트 입력 시작: {prompt}")
            
            # # Grok 앱이 간헐적으로 꺼질 수 있으므로 실행 확인 및 재실행
            # self._launch_grok_app(device_id)
            # time.sleep(3)  # 앱 실행 후 안정화 대기
            
            self.laixi_controller.execute_adb_command(device_id, "input tap 340 230")
            time.sleep(1)
            Logger.log(f"네트워크 문제 해결을 위한 탭이동")

            self.laixi_controller.execute_adb_command(device_id, "input tap 730 230")
            time.sleep(1)
            
            Logger.log(f"네트워크 문제 해결을 위한 탭이동")
            # self.laixi_controller.execute_adb_command(device_id, "input keyevent 207")
            # time.sleep(1)
            # self.laixi_controller.execute_adb_command(device_id, "input keyevent 207")
            # time.sleep(1)
            # 입력 필드 클릭 (410, 2050)
            Logger.log(f"[{device_id}] 입력 필드 클릭 (410, 2050)...")
            self.laixi_controller.execute_adb_command(device_id, "input tap 410 2050")
            time.sleep(2)
            
            # 방법 1: 클립보드 사용 (디바이스별로 안전하게 처리)
            Logger.log(f"[{device_id}] 클립보드에 프롬프트 저장...")
            clipboard_command = {
                "action": "writeclipboard",
                "comm": {
                    "deviceIds": device_id,  # 특정 디바이스만 지정
                    "content": prompt
                }
            }
            self.laixi_controller.send_command(json.dumps(clipboard_command))
            time.sleep(1)
            
            # # 롱프레스 후 붙여넣기 메뉴 클릭
            # Logger.log(f"[{device_id}] 입력 필드 롱프레스...")
            # self.laixi_controller.execute_adb_command(device_id, "input swipe 410 2050 410 2050 1000")
            # time.sleep(2)
            
            # # 붙여넣기 버튼 클릭 (좌표: 540, 1850 - 상황에 따라 다를 수 있음)
            # Logger.log(f"[{device_id}] 붙여넣기 버튼 클릭...")
            # self.laixi_controller.execute_adb_command(device_id, "input tap 540 1850")
            # time.sleep(2)
            
            # 방법 2: input text 사용 (영문만 가능, 한글은 안 됨)
            # Logger.log(f"[{device_id}] 프롬프트 직접 입력...")
            # # 한글은 URL 인코딩 또는 다른 방법 필요
            # self.laixi_controller.execute_adb_command(device_id, f'input text "{prompt}"')
            # time.sleep(2)
            
            Logger.log(f"[{device_id}] 프롬프트 입력 완료")
            return True
                
        except Exception as e:
            Logger.log(f"[{device_id}] 프롬프트 입력 실패: {e}")
            return False
    
    def _click_generate_button(self, device_id: str) -> bool:
        """프롬프트 전송 버튼 클릭 (좌표: 970, 2050)"""
        try:
            Logger.log(f"[{device_id}] 전송 버튼 클릭 (970, 2050)...")

            self.laixi_controller.execute_adb_command(device_id, "input keyevent 4")
            time.sleep(1)
            self.laixi_controller.execute_adb_command(device_id, "input tap 970 2050")
            time.sleep(1)
            Logger.log(f"[{device_id}] 전송 버튼 클릭 완료")
            return True
                
        except Exception as e:
            Logger.log(f"[{device_id}] 전송 버튼 클릭 실패: {e}")
            return False
    
    def _download_images(self, device_id: str, key: str = None) -> Optional[List[str]]:
        """
        이미지 다운로드 (새로운 좌표)
        291 712 클릭 -> 215 1710 클릭(다운) -> 
        785 712 클릭 -> 215 1710 클릭(다운) -> 
        291 1660 클릭 -> 215 1710 클릭(다운)
        """
        try:
            Logger.log(f"[{device_id}] 이미지 생성 완료 대기 중...")
            time.sleep(20)  # 이미지 생성 대기
            
            # 취소 체크 (다운로드 시작 전)
            if key and self._check_cancelled(key):
                Logger.log(f"[{device_id}] 작업이 취소되었습니다 (KEY={key}, 다운로드 시작 전)")
                raise Exception("작업이 취소되었습니다")
            
            Logger.log(f"[{device_id}] 이미지 다운로드 시작...")
            
            # 첫 번째 이미지: 291 712 클릭 -> 215 1710 클릭
            # 취소 체크
            if key and self._check_cancelled(key):
                Logger.log(f"[{device_id}] 작업이 취소되었습니다 (KEY={key}, 첫 번째 이미지 다운로드 전)")
                raise Exception("작업이 취소되었습니다")
            
            Logger.log(f"[{device_id}] 첫 번째 이미지 클릭 (291, 712)...")
            self.laixi_controller.execute_adb_command(device_id, "input tap 291 712")
            time.sleep(2)
            Logger.log(f"[{device_id}] 다운로드 버튼 클릭 (215, 1710)...")
            self.laixi_controller.execute_adb_command(device_id, "input tap 215 1710")
            time.sleep(3)
            self.laixi_controller.execute_adb_command(device_id, "input keyevent 4")
            time.sleep(1)
            
            # 두 번째 이미지: 785 712 클릭 -> 215 1710 클릭
            # 취소 체크
            if key and self._check_cancelled(key):
                Logger.log(f"[{device_id}] 작업이 취소되었습니다 (KEY={key}, 두 번째 이미지 다운로드 전)")
                raise Exception("작업이 취소되었습니다")
            
            Logger.log(f"[{device_id}] 두 번째 이미지 클릭 (785, 712)...")
            self.laixi_controller.execute_adb_command(device_id, "input tap 785 712")
            time.sleep(2)
            Logger.log(f"[{device_id}] 다운로드 버튼 클릭 (215, 1710)...")
            self.laixi_controller.execute_adb_command(device_id, "input tap 215 1710")
            time.sleep(3)
            self.laixi_controller.execute_adb_command(device_id, "input keyevent 4")
            time.sleep(1)
            
            # 세 번째 이미지: 291 1660 클릭 -> 215 1710 클릭
            # 취소 체크
            if key and self._check_cancelled(key):
                Logger.log(f"[{device_id}] 작업이 취소되었습니다 (KEY={key}, 세 번째 이미지 다운로드 전)")
                raise Exception("작업이 취소되었습니다")
            
            Logger.log(f"[{device_id}] 세 번째 이미지 클릭 (291, 1660)...")
            self.laixi_controller.execute_adb_command(device_id, "input tap 291 1660")
            time.sleep(2)
            Logger.log(f"[{device_id}] 다운로드 버튼 클릭 (215, 1710)...")
            self.laixi_controller.execute_adb_command(device_id, "input tap 215 1710")
            time.sleep(3)
            self.laixi_controller.execute_adb_command(device_id, "input keyevent 4")
            time.sleep(1)
            
            Logger.log(f"[{device_id}] 다운로드 완료 대기 중...")
            time.sleep(2)
            
            # 최근 다운로드된 이미지 파일 3개 찾기
            download_dir = "/sdcard/Download"
            list_cmd = f"ls -t {download_dir}/*.jpg {download_dir}/*.png {download_dir}/*.webp 2>/dev/null | head -3"
            result = self.laixi_controller.execute_adb_command(device_id, list_cmd)
            
            Logger.log(f"[{device_id}] 다운로드된 파일 확인 결과: {result}")
            
            # JSON 응답 파싱
            remote_image_paths = []
            
            if isinstance(result, dict):
                result_data = result.get('result', '')
                if isinstance(result_data, str):
                    try:
                        parsed_result = json.loads(result_data)
                        if isinstance(parsed_result, dict) and device_id in parsed_result:
                            file_list = parsed_result[device_id]
                            if isinstance(file_list, list) and len(file_list) > 0:
                                remote_image_paths = [f.strip() for f in file_list if f and f.strip()]
                                Logger.log(f"[{device_id}] 파싱 성공 - remote_image_paths: {remote_image_paths}")
                    except Exception as parse_error:
                        Logger.log(f"[{device_id}] JSON 파싱 오류: {parse_error}")
            
            if not remote_image_paths or len(remote_image_paths) < 3:
                Logger.log(f"[{device_id}] 다운로드 폴더에서 3개 이미지를 찾을 수 없습니다. 찾은 개수: {len(remote_image_paths)}")
                return None
            
            Logger.log(f"[{device_id}] 찾은 이미지 파일 3개: {remote_image_paths}")
            
            # 로컬 디렉토리 생성 (디바이스별 별도 폴더 - 동시 요청 충돌 방지)
            project_root = os.path.dirname(os.path.abspath(__file__))
            local_image_dir = os.path.join(project_root, 'image', device_id)  # 디바이스별 폴더
            os.makedirs(local_image_dir, exist_ok=True)
            Logger.log(f"[{device_id}] 로컬 이미지 저장 경로: {local_image_dir} (디바이스 전용 폴더)")
            
            # 3개 이미지 모두 로컬로 복제 (Laixi PullFile 액션 사용)
            local_image_paths = []
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            
            for idx, remote_image_path in enumerate(remote_image_paths[:3]):
                # 파일 확장자 유지
                file_ext = os.path.splitext(remote_image_path)[1] or '.jpg'
                local_image_path = os.path.join(local_image_dir, f"grok_image_{device_id}_{timestamp}_{idx+1}{file_ext}")
                
                # Laixi PullFile 액션으로 파일 다운로드
                Logger.log(f"[{device_id}] 파일 {idx+1}/3 다운로드 시작: {remote_image_path}")
                
                try:
                    # PullFile 명령어 구성
                    pull_command = {
                        "action": "PullFile",
                        "comm": {
                            "deviceIds": device_id,
                            "phoneFilePath": remote_image_path,
                            "savePath": local_image_path.replace('\\', '/')  # Windows 경로를 슬래시로 변환
                        }
                    }
                    
                    Logger.log(f"[{device_id}] PullFile 명령어: {json.dumps(pull_command)}")
                    result = self.laixi_controller.send_command(json.dumps(pull_command))
                    Logger.log(f"[{device_id}] PullFile 결과: {result}")
                    
                    # 파일 다운로드 대기
                    time.sleep(3)
                    
                    # 파일이 정상적으로 다운로드되었는지 확인
                    if os.path.exists(local_image_path) and os.path.getsize(local_image_path) > 0:
                        Logger.log(f"[{device_id}] 이미지 {idx+1}/3 로컬 저장 완료: {local_image_path} ({os.path.getsize(local_image_path)} bytes)")
                        local_image_paths.append(local_image_path)
                    else:
                        Logger.log(f"[{device_id}] 이미지 {idx+1}/3 파일 생성 실패: {local_image_path}")
                    
                except Exception as download_error:
                    Logger.log(f"[{device_id}] PullFile 오류 {idx+1}/3: {download_error}")
                    traceback.print_exc()
            
            # 디바이스에서 다운로드된 이미지 삭제
            Logger.log(f"[{device_id}] 디바이스에서 이미지 삭제 시작...")
            for remote_image_path in remote_image_paths[:3]:
                delete_cmd = f"rm {remote_image_path}"
                self.laixi_controller.execute_adb_command(device_id, delete_cmd)
                Logger.log(f"[{device_id}] 삭제 완료: {remote_image_path}")
            
            if len(local_image_paths) == 3:
                Logger.log(f"[{device_id}] 3개 이미지 모두 로컬 복제 완료")
                return local_image_paths
            else:
                Logger.log(f"[{device_id}] 일부 이미지만 복제됨: {len(local_image_paths)}/3")
                return None
            
        except Exception as e:
            Logger.log(f"[{device_id}] 이미지 다운로드 실패: {e}")
            traceback.print_exc()
            return None
    
    def _upload_to_cdn(self, image_path: str) -> Optional[str]:
        """FTP CDN에 이미지 업로드 (exmaple1.py 로직 참고)"""
        try:
            import ftplib
            from io import BytesIO
            import random
            
            Logger.log(f"[CDN] FTP 업로드 시작: {image_path}")
            
            if not os.path.exists(image_path):
                Logger.log(f"[CDN] 이미지 파일이 존재하지 않음: {image_path}")
                return None
            
            # FTP 설정 (exmaple1.py CDN_CONFIG 참고)
            ftp_host = "115.68.92.164"
            ftp_port = 21
            ftp_user = "coresolution1"
            ftp_pass = "nhw212821!"
            ftp_upload_path = "/images/dipseek/"
            base_url = "https://coresolution1.smilecast.co.kr/images/dipseek/"
            passive_mode = True
            
            # 파일명 생성 (타임스탬프 + 랜덤 ID)
            timestamp = int(time.time())
            random_id = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))
            file_ext = os.path.splitext(image_path)[1] or '.jpg'
            remote_filename = f"grok_{timestamp}_{random_id}{file_ext}"
            
            # 이미지 파일 읽기
            with open(image_path, 'rb') as f:
                image_binary = f.read()
            
            Logger.log(f"[CDN] 이미지 크기: {len(image_binary)} bytes")
            
            # FTP 연결 및 업로드
            ftp = None
            try:
                Logger.log(f"[CDN] FTP 서버 연결 중: {ftp_host}")
                
                ftp = ftplib.FTP()
                ftp.connect(ftp_host, ftp_port)
                ftp.login(ftp_user, ftp_pass)
                
                # 패시브 모드 설정
                if passive_mode:
                    ftp.set_pasv(True)
                
                Logger.log(f"[CDN] FTP 연결 성공")
                
                # 업로드 디렉토리로 이동
                upload_path = ftp_upload_path.strip('/')
                if upload_path:
                    try:
                        ftp.cwd('/')
                        for folder in upload_path.split('/'):
                            if folder:
                                try:
                                    ftp.cwd(folder)
                                except ftplib.error_perm:
                                    Logger.log(f"[CDN] 폴더 생성: {folder}")
                                    ftp.mkd(folder)
                                    ftp.cwd(folder)
                    except Exception as path_error:
                        Logger.log(f"[CDN] 경로 설정 실패: {path_error}")
                        Logger.log(f"[CDN] 루트 디렉토리에 업로드")
                
                # 파일 업로드
                bio = BytesIO(image_binary)
                Logger.log(f"[CDN] 파일 업로드 중: {remote_filename}")
                
                ftp.storbinary(f'STOR {remote_filename}', bio)
                
                # CDN URL 생성
                cdn_url = f"{base_url}{remote_filename}"
                Logger.log(f"[CDN] 업로드 성공: {cdn_url}")
                
                return cdn_url
                
            except ftplib.all_errors as ftp_error:
                Logger.log(f"[CDN] FTP 오류: {ftp_error}")
                return None
                
            except Exception as upload_error:
                Logger.log(f"[CDN] 업로드 오류: {upload_error}")
                return None
                
            finally:
                if ftp:
                    try:
                        ftp.quit()
                        Logger.log(f"[CDN] FTP 연결 종료")
                    except:
                        try:
                            ftp.close()
                        except:
                            pass
                    
        except Exception as e:
            Logger.log(f"[CDN] 업로드 중 오류 발생: {e}")
            traceback.print_exc()
            return None
    
    def _send_callback(self, device_id: str, prompt: str, cdn_urls: list, key: str = None, uniq: str = None, callback_url: str = None):
        """메인 서버로 콜백 전송 (exmaple2.py 형식 참고)"""
        try:
            import requests
            
            # 콜백 URL 결정 및 유효성 검사
            target_url = callback_url or self.cdn_callback_url
            
            # URL 유효성 검사
            if not target_url or not (target_url.startswith('http://') or target_url.startswith('https://')):
                Logger.log(f"[{device_id}] ERROR: 유효하지 않은 콜백 URL: {target_url}")
                Logger.log(f"  - callback_url 파라미터: {callback_url}")
                Logger.log(f"  - self.cdn_callback_url: {self.cdn_callback_url}")
                Logger.log(f"  - 기본 콜백 URL 사용: {self.cdn_callback_url}")
                target_url = self.cdn_callback_url
            
            Logger.log(f"[{device_id}] 콜백 전송 준비:")
            Logger.log(f"  - callback_url 파라미터: {callback_url}")
            Logger.log(f"  - self.cdn_callback_url: {self.cdn_callback_url}")
            Logger.log(f"  - 최종 target_url: {target_url}")
            Logger.log(f"  - CDN URLs ({len(cdn_urls)}개): {cdn_urls}")
            
            # 콜백 페이로드 구성 (exmaple2.py 형식)
            # images를 정상 형식(객체 배열)으로 변환
            images_formatted = [
                {
                    "id": f"img_{i+1}",
                    "url": url,
                    "filename": url.split('/')[-1],
                    "size_kb": 0,  # 크기 정보 없음 (필요시 추가 가능)
                    "method": "FTP"
                }
                for i, url in enumerate(cdn_urls)
            ]
            
            callback_payload = {
                "key": key or "",
                "uniq": uniq or "",
                "callback": target_url,
                "state": "completed",
                "data": {
                    "images": images_formatted,  # 객체 배열 형식 (정상 콜백 형식)
                    "translation": {
                        "original": prompt,
                        "translated": prompt  # 번역은 Grok에서 직접 처리
                    },
                    "metadata": {
                        "prompt": prompt,
                        "device_id": device_id,
                        "timestamp": datetime.now().isoformat()
                    }
                },
                "error": None,
                "message": "이미지 생성이 성공적으로 완료되었습니다.",
                "processing_time_seconds": 0
            }
            
            # Form 데이터 준비 (exmaple2.py send_callback 함수 참고)
            json_data = json.dumps(callback_payload, ensure_ascii=False)
            form_data = {
                'key': key or "",
                'uniq': uniq or "",
                'json': json_data
            }
            
            Logger.log(f"[{device_id}] 콜백 전송: {target_url}")
            Logger.log(f"[{device_id}] 콜백 데이터: key={key}, uniq={uniq}")
            
            # 콜백 URL로 POST 요청 (Form 데이터)
            response = requests.post(
                target_url,
                data=form_data,
                timeout=10
            )
            
            if response.status_code == 200:
                Logger.log(f"[{device_id}] 콜백 전송 성공: {response.status_code}")
                return True
            else:
                Logger.log(f"[{device_id}] 콜백 전송 실패 (HTTP {response.status_code}): {response.text}")
                return False
                
        except Exception as e:
            Logger.log(f"[{device_id}] 콜백 전송 중 오류: {e}")
            traceback.print_exc()
            return False
    
    def get_device_status(self) -> Dict[str, Dict]:
        """모든 디바이스의 현재 상태 반환"""
        return self.device_status.copy()
    
    def get_busy_devices(self) -> List[str]:
        """현재 작업 중인 디바이스 목록 반환"""
        return [device_id for device_id, status in self.device_status.items() 
               if status.get('status') == 'busy']
    
    def get_idle_devices(self) -> List[str]:
        """현재 유휴 상태인 디바이스 목록 반환"""
        return [device_id for device_id, status in self.device_status.items() 
               if status.get('status') == 'idle']

    def allocate_idle_device(self, prompt: str = None) -> Optional[str]:
        """유휴 디바이스 하나를 즉시 'busy'로 예약하여 반환 (원자적, 라운드 로빈)
        - 동시 요청 경합 시 같은 디바이스가 중복 배정되는 문제를 방지
        - 라운드 로빈 방식으로 디바이스를 순환하며 할당
        """
        with self._allocation_lock:
            device_list = list(self.device_status.keys())
            if not device_list:
                return None
            
            # 라운드 로빈: 마지막 할당 인덱스 다음부터 순회
            total_devices = len(device_list)
            for i in range(total_devices):
                # 다음 인덱스 계산 (순환)
                next_index = (self._last_allocated_index + 1 + i) % total_devices
                device_id = device_list[next_index]
                status = self.device_status[device_id]
                
                if status.get('status') == 'idle':
                    # 즉시 busy로 마킹하여 경합 방지
                    self.device_status[device_id]['status'] = 'busy'
                    self.device_status[device_id]['current_task'] = prompt or 'background_task'
                    self.device_status[device_id]['last_activity'] = datetime.now()
                    self._last_allocated_index = next_index  # 마지막 할당 인덱스 업데이트
                    Logger.log(f"[ALLOC] 디바이스 예약 (라운드 로빈 #{next_index}): {device_id}")
                    return device_id
        
        return None
    
    def stop_all_tasks(self):
        """모든 진행 중인 작업 중단 (동기 방식)"""
        Logger.log("모든 작업 중단 요청")
        for device_id in self.get_busy_devices():
            self.device_status[device_id]['status'] = 'idle'
            self.device_status[device_id]['current_task'] = None
            Logger.log(f"[{device_id}] 작업 중단됨")
    
    def get_device_statistics(self) -> Dict:
        """디바이스 통계 정보 반환"""
        total_devices = len(self.device_status)
        idle_devices = len(self.get_idle_devices())
        busy_devices = len(self.get_busy_devices())
        
        error_devices = [device_id for device_id, status in self.device_status.items() 
                        if status.get('error_count', 0) > 0]
        
        return {
            "total_devices": total_devices,
            "idle_devices": idle_devices,
            "busy_devices": busy_devices,
            "error_devices": len(error_devices),
            "error_device_list": error_devices,
            "utilization_rate": f"{(busy_devices/total_devices*100):.1f}%" if total_devices > 0 else "0%"
        }
