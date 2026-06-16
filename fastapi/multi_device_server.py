# 코드 학습 목표
# 엔드포인트 파악 - 이미지 생성 / 작업 제어 / 상태 조회 / 프록시 관리
# 콜백 / 이미지 CDN 업로드 흐름
# 디스패쳐 / 스레드 / 큐 


# FastAPI 애플리케이션 초기화
# 단일 스레드 이벤트 루프 - 하나의 스레드가 요청을 번갈아서 처리
# 
app = FastAPI(
    title="Multi-Device Grok Image Generation API",
    description="20대 폰보드를 활용하여 Grok AI로 이미지를 병렬 생성하고 CDN에 저장 후 콜백을 전송하는 서비스",
    version="2.0.0"
)

# Laixi ADB WebSocket 연결 정보
LAIXI_WS_URL = 'ws://127.0.0.1:22221'
CDN_CALLBACK_URL = 'http://127.0.0.1:9000/callback'  # 기본 콜백 URL (테스트용)

# 프록시 사용 여부 설정 (False = 프록시 사용-
#  안 함, True = 프록시 사용)
ENABLE_PROXY = True  # ← 프록시 비활성화

# 전역 변수 - global 전역 싱글톤, 서버가 뜨면 객체로 채운다.
laixi_controller = None
multi_device_manager = None
proxy_manager = None
perchance_generator: Optional[PerchanceGenerator] = None

# 작업 상태 추적을 위한 전역 딕셔너리
active_tasks = {}  # 작업 단위 상태 저장 {key: {"status": "processing", "cancelled": False, "device_id": "xxx"}}
active_uniq_tasks = {}  # 회원 단위 보조 인덱스스 {uniq: {"key": "abc123", "start_time": datetime, "status": "processing"}}

# 태스크 큐 (디바이스 부족 시 대기열)
from collections import deque
task_deque: deque = deque()
task_queue_lock = threading.Lock() # 동시 접근 충돌 방지(상호 배제)
dispatcher_event = threading.Event() # 작업이 생겼을 때 디스패처를 꺠우는 신호

def apply_proxy_to_device(device_id, proxy):
    """특정 디바이스에 프록시 적용"""
    global laixi_controller
    
    try:
        if not laixi_controller:
            return False
        
        if proxy:
            # global HTTP proxy 설정
            proxy_host, proxy_port = proxy.split(':')
            cmd = f"settings put global http_proxy {proxy_host}:{proxy_port}"
            laixi_controller.execute_adb_command(device_id, cmd)
            Logger.log(f"[{device_id}] 프록시 적용: {proxy}")
            return True
        else:
            # 프록시 해제
            cmd = "settings put global http_proxy :0"
            laixi_controller.execute_adb_command(device_id, cmd)
            Logger.log(f"[{device_id}] 프록시 해제")
            return True
    except Exception as e:
        Logger.log(f"[{device_id}] 프록시 적용 실패: {e}")
        return False

def apply_proxy_settings():
    """저장된 프록시 설정을 각 디바이스에 적용 (또는 자동 할당)"""
    global laixi_controller, proxy_manager
    
    try:
        if not laixi_controller or not proxy_manager:
            return
        
        devices = laixi_controller.get_devices()
        applied_count = 0
        
        for device in devices:
            device_id = device.get('name', '') or device.get('deviceId', '')
            if not device_id:
                continue
            
            # 현재 할당된 프록시 확인
            proxy = proxy_manager.get_proxy(device_id)
            
            # 프록시가 없으면 풀에서 자동 할당
            if not proxy and proxy_manager.proxy_pool:
                proxy = proxy_manager.get_next_proxy_from_pool()
                if proxy:
                    proxy_manager.set_proxy(device_id, proxy)
                    Logger.log(f"[{device_id}] 프록시 자동 할당: {proxy}")
            
            # 프록시 적용
            if proxy:
                if apply_proxy_to_device(device_id, proxy):
                    applied_count += 1
        
        if applied_count > 0:
            Logger.log(f"{applied_count}개 디바이스에 프록시 적용 완료")
        else:
            Logger.log("프록시가 적용된 디바이스 없음")
        
    except Exception as e:
        Logger.log(f"프록시 설정 적용 중 오류: {e}")
        traceback.print_exc()

def check_and_rotate_proxy(device_id):
    """프록시 상태 확인 후 필요시 교체"""
    global proxy_manager, laixi_controller
    
    try:
        if not proxy_manager:
            return False
        
        current_proxy = proxy_manager.get_proxy(device_id)
        if not current_proxy:
            return False
        
        Logger.log(f"[{device_id}] 프록시 연결 테스트 중: {current_proxy}")
        
        # 프록시 테스트 (3초 타임아웃)
        if not proxy_manager.test_proxy(current_proxy, timeout=3):
            Logger.log(f"[{device_id}] 프록시 연결 실패, 교체 시작: {current_proxy}")
            
            # 최대 5번까지 다른 프록시로 시도
            max_retries = 5
            for attempt in range(max_retries):
                new_proxy = proxy_manager.rotate_proxy(device_id)
                if not new_proxy:
                    Logger.log(f"[{device_id}] 사용 가능한 프록시가 없음")
                    return False
                
                Logger.log(f"[{device_id}] 새 프록시 테스트 중 ({attempt+1}/{max_retries}): {new_proxy}")
                
                # 새 프록시 테스트
                if proxy_manager.test_proxy(new_proxy, timeout=3):
                    Logger.log(f"[{device_id}] 프록시 교체 성공: {new_proxy}")
                    apply_proxy_to_device(device_id, new_proxy)
                    return True
                else:
                    Logger.log(f"[{device_id}] 프록시 연결 실패: {new_proxy}")
            
            Logger.log(f"[{device_id}] 모든 프록시 교체 시도 실패")
            return False
        else:
            Logger.log(f"[{device_id}] 프록시 정상: {current_proxy}")
            return True
            
    except Exception as e:
        Logger.log(f"[{device_id}] 프록시 체크 중 오류: {e}")
        traceback.print_exc()
        return False

def send_callback(callback_url: str, response_data: dict):
    """콜백 URL로 결과를 전송하는 함수 (POST 방식 + Form 데이터)"""
    try:
        # Form 데이터 준비
        key = response_data.get('key', '')
        uniq = response_data.get('uniq', '')
        
        # JSON 데이터를 문자열로 변환
        json_data = json.dumps(response_data, ensure_ascii=False)
        
        # Form 데이터 생성
        form_data = {
            'key': key,
            'uniq': uniq,
            'json': json_data
        }
        
        Logger.log(f"Callback send: {callback_url} (KEY: {key}, State: {response_data.get('state')})")
        
        # 콜백 요청 전송 (POST 방식, Form 데이터, 5초 타임아웃)
        response = requests.post(
            callback_url,
            data=form_data,
            timeout=5,
            verify=False,
        )
        
        if response.status_code == 200:
            Logger.log(f"Callback success (status: {response.status_code})")
        else:
            Logger.log(f"Callback failed (status: {response.status_code})")
            
    except requests.exceptions.Timeout:
        Logger.log(f"Callback timeout: {callback_url}")
    except requests.exceptions.RequestException as e:
        Logger.log(f"Callback error: {callback_url} - {e}")
    except Exception as e:
        Logger.log(f"Callback critical error: {e}")

def background_perchance_task(key: str, uniq: str, callback: str, prompt: str, count: int, shape: str, pre_allocated_device: str = None):
    """백그라운드에서 Perchance 이미지 생성을 처리하는 함수 (디스패처가 device를 미리 할당)"""
    start_time = active_tasks.get(key, {}).get("start_time") or datetime.now()
    selected_device = pre_allocated_device
    try:
        active_tasks[key] = {
            "status": "processing", "cancelled": False,
            "uniq": uniq, "start_time": start_time,
            "device_id": selected_device, "callback": callback, "prompt": prompt,
        }
        active_uniq_tasks[uniq] = {
            "key": key, "start_time": start_time,
            "status": "processing", "device_id": selected_device,
        }

        def check_cancelled():
            return key in active_tasks and active_tasks[key].get("cancelled", False)

        result = perchance_generator.generate_with_params(
            device_id=selected_device,
            prompt=prompt,
            count=count,
            shape=shape,
            key=key,
            check_cancelled=check_cancelled,
        )

        processing_time = (datetime.now() - start_time).total_seconds()

        if result["status"] != "completed":
            raise Exception(result.get("message", "이미지 생성 실패"))

        send_callback(callback, {
            "key": key, "uniq": uniq, "callback": callback,
            "state": "completed",
            "data": {
                "translation": {
                    "original": prompt,
                    "translated": result.get("translated_prompt"),
                },
                "images": result["images"],
            },
            "message": result["message"],
            "processing_time_seconds": processing_time,
        })

        active_tasks[key]["status"] = "completed"
        if uniq in active_uniq_tasks:
            del active_uniq_tasks[uniq]
        multi_device_manager.remove_cancelled(key)

    except Exception as e:
        processing_time = (datetime.now() - start_time).total_seconds()
        send_callback(callback, {
            "key": key, "uniq": uniq, "callback": callback,
            "state": "error",
            "data": {"translation": {"original": prompt, "translated": None}},
            "error": {"code": "INTERNAL_ERROR", "message": str(e), "details": traceback.format_exc()},
            "message": "서버 내부 오류",
            "processing_time_seconds": processing_time,
        })
        if key in active_tasks:
            active_tasks[key]["status"] = "failed"
        if uniq in active_uniq_tasks:
            del active_uniq_tasks[uniq]
        if multi_device_manager:
            multi_device_manager.remove_cancelled(key)
    finally:
        if selected_device and multi_device_manager:
            if selected_device in multi_device_manager.device_status:
                multi_device_manager.device_status[selected_device]["status"] = "idle"
                multi_device_manager.device_status[selected_device]["current_task"] = None
        dispatcher_event.set()  # 디바이스 해제 → 디스패처 깨우기

# 딥시크프로젝트용 번역 후 큐에 삽입 함수
def _deepseek_translate_and_enqueue(key: str, uniq: str, callback: str, raw_prompt: str, count: int, queued_at: datetime):
    """딥시크: 한국어 번역 완료 후 큐에 삽입. 번역 실패 시 에러 콜백."""
    try:
        translated = perchance_generator.translate_to_english(raw_prompt)
        Logger.log(f"[{key}] 번역 완료 → 큐 삽입: {translated[:60]}")
        task = enqueue_task({
            "task_type": "deepseek",
            "key": key, "uniq": uniq, "callback": callback,
            "prompt": translated,
            "raw_prompt": raw_prompt,
            "count": count,
            "queued_at": queued_at,
        })
        # 원본 프롬프트를 active_tasks에 보존 (콜백 전송 시 사용)
        if key in active_tasks:
            active_tasks[key]["raw_prompt"] = raw_prompt
    except Exception as e:
        Logger.log(f"[{key}] 번역 실패: {e}")
        if key in active_tasks:
            del active_tasks[key]
        if uniq in active_uniq_tasks:
            del active_uniq_tasks[uniq]
        send_callback(callback, {
            "key": key, "uniq": uniq, "callback": callback,
            "state": "error",
            "data": {"translation": {"original": raw_prompt, "translated": None}},
            "error": {"code": "TRANSLATION_ERROR", "message": str(e)},
            "message": "번역 실패",
            "processing_time_seconds": (datetime.now() - queued_at).total_seconds(),
        })


def background_deepseek_task(key: str, uniq: str, callback: str, translated_prompt: str, count: int, pre_allocated_device: str = None):
    """딥시크용: 번역 완료된 프롬프트로 Perchance 이미지 생성 (디스패처가 device 미리 할당)."""
    start_time = active_tasks.get(key, {}).get("start_time") or datetime.now()
    raw_prompt = active_tasks.get(key, {}).get("raw_prompt", translated_prompt)
    selected_device = pre_allocated_device
    try:
        active_tasks[key] = {
            "status": "processing", "cancelled": False,
            "uniq": uniq, "start_time": start_time,
            "device_id": selected_device, "callback": callback,
            "prompt": translated_prompt, "raw_prompt": raw_prompt,
        }
        active_uniq_tasks[uniq] = {
            "key": key, "start_time": start_time,
            "status": "processing", "device_id": selected_device,
        }

        def check_cancelled():
            return key in active_tasks and active_tasks[key].get("cancelled", False)

        result = perchance_generator.generate_with_params(
            device_id=selected_device,
            prompt=translated_prompt,
            count=count,
            shape="portrait",
            key=key,
            check_cancelled=check_cancelled,
        )

        processing_time = (datetime.now() - start_time).total_seconds()
        if result["status"] != "completed":
            raise Exception(result.get("message", "이미지 생성 실패"))

        send_callback(callback, {
            "key": key, "uniq": uniq, "callback": callback,
            "state": "completed",
            "data": {
                "translation": {"original": raw_prompt, "translated": translated_prompt},
                "images": result["images"],
            },
            "message": result["message"],
            "processing_time_seconds": processing_time,
        })

        active_tasks[key]["status"] = "completed"
        if uniq in active_uniq_tasks:
            del active_uniq_tasks[uniq]
        multi_device_manager.remove_cancelled(key)

    except Exception as e:
        processing_time = (datetime.now() - start_time).total_seconds()
        send_callback(callback, {
            "key": key, "uniq": uniq, "callback": callback,
            "state": "error",
            "data": {"translation": {"original": raw_prompt, "translated": translated_prompt}},
            "error": {"code": "INTERNAL_ERROR", "message": str(e), "details": traceback.format_exc()},
            "message": "서버 내부 오류",
            "processing_time_seconds": processing_time,
        })
        if key in active_tasks:
            active_tasks[key]["status"] = "failed"
        if uniq in active_uniq_tasks:
            del active_uniq_tasks[uniq]
        if multi_device_manager:
            multi_device_manager.remove_cancelled(key)
    finally:
        if selected_device and multi_device_manager:
            if selected_device in multi_device_manager.device_status:
                multi_device_manager.device_status[selected_device]["status"] = "idle"
                multi_device_manager.device_status[selected_device]["current_task"] = None
        dispatcher_event.set()  # 디바이스 해제 → 디스패처 깨우기


def enqueue_task(task: dict) -> dict:
    """태스크를 대기열 끝에 추가하고 디스패처를 깨운다."""
    key = task["key"]
    queued_at = task.get("queued_at") or datetime.now()
    active_tasks[key] = {
        "status": "queued",
        "cancelled": False,
        "uniq": task["uniq"],
        "start_time": queued_at,
        "device_id": None,
        "callback": task["callback"],
        "prompt": task.get("prompt", ""),
    }
    active_uniq_tasks[task["uniq"]] = {
        "key": key,
        "start_time": queued_at,
        "status": "queued",
        "device_id": None,
    }
    with task_queue_lock:
        task_deque.append(task)
    Logger.log(f"[QUEUE] 등록: key={key}, type={task['task_type']}, 대기={len(task_deque)}")
    dispatcher_event.set()
    return task


def _spawn_task_thread(task: dict, pre_device: str):
    """디스패처가 호출 — pre_device를 받아 백그라운드 태스크 스레드를 시작한다."""
    tt = task["task_type"]
    if tt == "perchance":
        fn = background_perchance_task
        args = (task["key"], task["uniq"], task["callback"],
                task["prompt"], task["count"], task["shape"], pre_device)
    elif tt == "deepseek":
        fn = background_deepseek_task
        args = (task["key"], task["uniq"], task["callback"],
                task["prompt"], task["count"], pre_device)
    else:
        Logger.log(f"[QUEUE] 알 수 없는 태스크 타입: {tt}")
        return
    t = threading.Thread(target=fn, args=args)
    t.daemon = True
    t.start()


def task_dispatcher():
    """디바이스 해제 시 대기열에서 태스크를 꺼내 배정하는 단일 디스패처 스레드."""
    Logger.log("[QUEUE] 디스패처 시작")
    while True: # 바깥 - 신호 받고 깨어나기
        dispatcher_event.wait()
        dispatcher_event.clear() # 깃발 리셋

        while True: # 안쪽 - 기기 있는 만큼 연속 배정
            with task_queue_lock:
                if not task_deque:
                    break
                task = task_deque[0]  # peek

            key = task["key"]

            # 취소된 태스크 버리고 건너뛰기
            if active_tasks.get(key, {}).get("cancelled", False):
                with task_queue_lock:
                    task_deque.popleft()  # 기기 배정 없이 버림
                Logger.log(f"[QUEUE] 취소 태스크 스킵: {key}")
                continue # 다음 작업으로로

            # 디바이스 할당 시도
            if not multi_device_manager:
                break
            pre_device = multi_device_manager.allocate_idle_device(task.get("prompt", "")) # 빈기기 확보 시도
            if not pre_device:
                Logger.log(f"[QUEUE] 디바이스 부족, 대기 (큐={len(task_deque)})")
                break  # 다음 해제 신호까지 대기

            # 할당 성공 → 큐에서 제거 후 스레드 실행
            with task_queue_lock:
                task_deque.popleft() # 기기 잡았으니 큐에서 제거거
            Logger.log(f"[QUEUE] 디스패치: key={key}, type={task['task_type']}, device={pre_device}")
            _spawn_task_thread(task, pre_device)


@app.on_event("startup")
async def startup_event():
    # 전역 객체 초기화
    global laixi_controller, multi_device_manager, proxy_manager, perchance_generator
    Logger.log("FastAPI startup event: Initializing Multi-Device Grok Manager")
    try:
        # 프록시 매니저 초기화 (ENABLE_PROXY가 True일 때만)
        if ENABLE_PROXY:
            proxy_manager = ProxyManager()
            Logger.log("Proxy Manager initialized (프록시 활성화)")
        else:
            Logger.log("Proxy Manager disabled (프록시 비활성화)")
        
        laixi_controller = LaixiAdbController(LAIXI_WS_URL)
        
        # WebSocket 연결
        try:
            laixi_controller.connect()
            Logger.log("WebSocket connected successfully")
        except Exception as conn_error:
            Logger.log(f"WebSocket connection failed: {conn_error}")
        
        multi_device_manager = MultiDeviceGrokManager(laixi_controller, CDN_CALLBACK_URL)

        # 디바이스 초기화 (동기 방식)
        device_ids = multi_device_manager.initialize_devices()
        Logger.log(f"Multi-Device Grok Manager initialized with {len(device_ids)} devices")

        perchance_generator = PerchanceGenerator(laixi_controller)
        Logger.log("PerchanceGenerator initialized")

        # 서버 시작 시 좌표 캐시 초기화
        import glob as _glob
        coords_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coords")
        cache_files = _glob.glob(os.path.join(coords_dir, "perchance_*.json"))
        for f in cache_files:
            try:
                os.remove(f)
            except Exception:
                pass
        Logger.log(f"좌표 캐시 초기화 완료: {len(cache_files)}개 파일 삭제")

        # 백그라운드에서 모든 기기 일일 캘리브레이션 (순차 실행)
        def calibrate_all_bg():
            try:
                devices = laixi_controller.get_devices()
                Logger.log(f"캘리브레이션 시작: {len(devices)}개 기기")
                for device in devices:
                    device_id = device.get('name') or device.get('deviceId')
                    if not device_id:
                        continue

                    status = multi_device_manager.device_status.get(device_id, {}).get('status', 'idle')
                    if status != 'idle':
                        Logger.log(f"[{device_id}] 캘리브레이션 스킵 (상태: {status})")
                        continue

                    multi_device_manager.device_status[device_id]['status'] = 'busy'
                    multi_device_manager.device_status[device_id]['current_task'] = 'calibration'
                    try:
                        perchance_generator.calibrate_device(device_id)
                    except Exception as e:
                        Logger.log(f"[{device_id}] 캘리브레이션 오류: {e}")
                    finally:
                        multi_device_manager.device_status[device_id]['status'] = 'idle'
                        multi_device_manager.device_status[device_id]['current_task'] = None

                Logger.log("전체 캘리브레이션 완료")
            except Exception as e:
                Logger.log(f"캘리브레이션 스레드 오류: {e}")

        threading.Thread(target=calibrate_all_bg, daemon=True).start()

        # 매일 자정(00:30) 전체 기기 재캘리브레이션 루프
        def midnight_calibration_loop():
            while True:
                now = datetime.now()
                next_run = (now + timedelta(days=1)).replace(hour=0, minute=30, second=0, microsecond=0)
                sleep_sec = (next_run - now).total_seconds()
                Logger.log(f"다음 자정 캘리브레이션: {next_run.strftime('%Y-%m-%d %H:%M')} ({sleep_sec/3600:.1f}시간 후)")
                time.sleep(sleep_sec)
                calibrate_all_bg()

        threading.Thread(target=midnight_calibration_loop, daemon=True).start()

        # 태스크 디스패처 시작
        threading.Thread(target=task_dispatcher, daemon=True).start()
        Logger.log("[QUEUE] 태스크 디스패처 시작")

        # 프록시 설정 자동 적용 (ENABLE_PROXY가 True일 때만)
        if ENABLE_PROXY:
            apply_proxy_settings()
        else:
            Logger.log("프록시 설정을 건너뜁니다 (ENABLE_PROXY=False)")
        
    except Exception as e:
        Logger.log(f"Failed to initialize Multi-Device Grok Manager: {e}")
        traceback.print_exc()

@app.on_event("shutdown")
async def shutdown_event():
    global laixi_controller, multi_device_manager
    if laixi_controller:
        Logger.log("FastAPI shutdown event: Disconnecting LaixiAdController websocket.")
        laixi_controller.disconnect()
        Logger.log("LaixiAdController websocket disconnected.")
    
    if multi_device_manager:
        multi_device_manager.stop_all_tasks()
        Logger.log("All tasks stopped.")

@app.get("/")
async def root():
    """API 루트 엔드포인트"""
    return {
        "message": "Multi-Device Grok Image Generation API",
        "version": "2.0.0",
        "description": "20대 폰보드를 활용한 병렬 이미지 생성 서비스 (프록시 자동 관리 지원)",
        "endpoints": {
            "GET /": "API 정보",
            "GET /health": "서버 상태 확인",
            "POST /suc/generator": "비동기 이미지 생성 (본서버 형식)",
            "POST /cancel": "작업 취소",
            "GET /devices": "디바이스 상태 조회",
            "GET /devices/status": "디바이스 통계 정보",
            "POST /devices/stop-all": "모든 작업 중단",
            "GET /proxy/settings": "프록시 설정 조회",
            "POST /proxy/apply": "프록시 설정 적용",
            "POST /proxy/rotate/{device_id}": "특정 디바이스 프록시 교체",
            "POST /proxy/check/{device_id}": "특정 디바이스 프록시 상태 확인",
            "POST /proxy/check-all": "모든 디바이스 프록시 상태 확인 및 자동 교체"
        },
        "proxy_info": {
            "pool_size": len(proxy_manager.proxy_pool) if proxy_manager else 0,
            "assigned_devices": len(proxy_manager.proxy_settings) if proxy_manager else 0
        },
        "status": "running"
    }

@app.get("/health")
async def health_check():
    """헬스 체크 엔드포인트"""
    return {
        "status": "ok",
        "devices": multi_device_manager.get_device_status() if multi_device_manager else {},
        "timestamp": datetime.now().isoformat()
    }

@app.get("/queue/status")
async def get_queue_status():
    """태스크 대기열 현황 조회"""
    with task_queue_lock:
        queued = [
            {"key": t["key"], "type": t["task_type"],
             "queued_at": t["queued_at"].isoformat() if t.get("queued_at") else None}
            for t in task_deque
        ]
    processing = [
        {"key": k, "device": v.get("device_id"), "prompt": v.get("prompt", "")[:40]}
        for k, v in active_tasks.items() if v.get("status") == "processing"
    ]
    return {
        "queue_size": len(queued),
        "processing_count": len(processing),
        "queued_tasks": queued,
        "processing_tasks": processing,
    }


@app.get("/proxy/settings")
async def get_proxy_settings():
    """프록시 설정 조회"""
    try:
        if not proxy_manager:
            return {"error": "Proxy manager not initialized"}
        
        return {
            "status": "ok",
            "proxy_settings": proxy_manager.proxy_settings,
            "proxy_pool_size": len(proxy_manager.proxy_pool),
            "proxy_pool_sample": proxy_manager.proxy_pool[:10] if proxy_manager.proxy_pool else []
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/proxy/apply")
async def apply_proxy():
    """프록시 설정 적용"""
    try:
        apply_proxy_settings()
        return {
            "status": "ok",
            "message": "Proxy settings applied successfully"
        }
    except Exception as e:
        Logger.log(f"Error applying proxy: {e}")
        return {"error": str(e)}

@app.post("/proxy/rotate/{device_id}")
async def rotate_device_proxy(device_id: str):
    """특정 디바이스의 프록시 강제 교체"""
    try:
        if not proxy_manager:
            return {"error": "Proxy manager not initialized"}
        
        new_proxy = proxy_manager.rotate_proxy(device_id)
        if new_proxy:
            apply_proxy_to_device(device_id, new_proxy)
            return {
                "status": "ok",
                "device_id": device_id,
                "new_proxy": new_proxy,
                "message": "Proxy rotated successfully"
            }
        else:
            return {
                "status": "error",
                "device_id": device_id,
                "message": "No available proxy in pool"
            }
    except Exception as e:
        Logger.log(f"Error rotating proxy: {e}")
        return {"error": str(e)}

@app.post("/proxy/check/{device_id}")
async def check_device_proxy(device_id: str):
    """특정 디바이스의 프록시 상태 확인"""
    try:
        if not proxy_manager:
            return {"error": "Proxy manager not initialized"}
        
        current_proxy = proxy_manager.get_proxy(device_id)
        if not current_proxy:
            return {
                "status": "no_proxy",
                "device_id": device_id,
                "message": "No proxy assigned"
            }
        
        is_working = proxy_manager.test_proxy(current_proxy, timeout=5)
        
        return {
            "status": "ok" if is_working else "failed",
            "device_id": device_id,
            "current_proxy": current_proxy,
            "is_working": is_working,
            "message": "Proxy is working" if is_working else "Proxy is not responding"
        }
    except Exception as e:
        Logger.log(f"Error checking proxy: {e}")
        return {"error": str(e)}

@app.post("/proxy/check-all")
async def check_all_proxies():
    """모든 디바이스의 프록시 상태 확인 및 자동 교체"""
    try:
        if not proxy_manager or not multi_device_manager:
            return {"error": "Services not initialized"}
        
        results = {}
        devices = laixi_controller.get_devices()
        
        for device in devices:
            device_id = device.get('name', '') or device.get('deviceId', '')
            if not device_id:
                continue
            
            proxy_rotated = check_and_rotate_proxy(device_id)
            current_proxy = proxy_manager.get_proxy(device_id)
            
            results[device_id] = {
                "current_proxy": current_proxy,
                "checked": True,
                "rotated": proxy_rotated
            }
        
        return {
            "status": "ok",
            "results": results,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        Logger.log(f"Error checking all proxies: {e}")
        traceback.print_exc()
        return {"error": str(e)}

@app.post("/suc/generator")
async def async_generate_grok_image(
    key: str = Form(..., description="작업 고유 번호"), # 폼 파라미터로 요청청 
    uniq: str = Form(..., description="회원 디비 색인번호"),
    callback: str = Form(..., description="콜백 URL"),
    prompt: str = Form(..., description="이미지 생성 프롬프트"),
    count: int = Form(default=1, description="생성할 이미지 개수")
):
    """비동기 Grok 이미지 생성 API (본서버 형식)"""
    start_time = datetime.now()
    
    try:
        # multi_device_manager 초기화 확인
        if not multi_device_manager:
            Logger.log("ERROR: Multi-Device Manager not initialized")
            return {
                "key": key,
                "uniq": uniq,
                "callback": callback,
                "state": "error",
                "error": {
                    "code": "SERVICE_NOT_READY",
                    "message": "서비스가 초기화되지 않았습니다. 잠시 후 다시 시도해주세요."
                },
                "message": "서비스 초기화 중",
                "processing_time_seconds": (datetime.now() - start_time).total_seconds()
            }
        
        Logger.log(f"Async image generation request received:")
        Logger.log(f"  - KEY: {key}")
        Logger.log(f"  - UNIQ: {uniq}")
        Logger.log(f"  - Callback URL: {callback}")
        Logger.log(f"  - Prompt: {prompt}")
        Logger.log(f"  - Count: {count}")
        
        # 유효성 검사
        if not key.strip():
            return {
                "key": key,
                "uniq": uniq,
                "callback": callback,
                "state": "error",
                "error": {
                    "code": "INVALID_KEY",
                    "message": "KEY가 제공되지 않았습니다."
                },
                "message": "잘못된 KEY입니다.",
                "processing_time_seconds": (datetime.now() - start_time).total_seconds()
            }
        
        if not uniq.strip():
            return {
                "key": key,
                "uniq": uniq,
                "callback": callback,
                "state": "error",
                "error": {
                    "code": "INVALID_UNIQ",
                    "message": "UNIQ가 제공되지 않았습니다."
                },
                "message": "잘못된 UNIQ입니다.",
                "processing_time_seconds": (datetime.now() - start_time).total_seconds()
            }
        
        if not callback.strip():
            return {
                "key": key,
                "uniq": uniq,
                "callback": callback,
                "state": "error",
                "error": {
                    "code": "INVALID_CALLBACK",
                    "message": "콜백 URL이 제공되지 않았습니다."
                },
                "message": "잘못된 콜백 URL입니다.",
                "processing_time_seconds": (datetime.now() - start_time).total_seconds()
            }
        
        if not prompt.strip():
            return {
                "key": key,
                "uniq": uniq,
                "callback": callback,
                "state": "error",
                "error": {
                    "code": "INVALID_PROMPT",
                    "message": "프롬프트가 제공되지 않았습니다."
                },
                "message": "잘못된 프롬프트입니다.",
                "processing_time_seconds": (datetime.now() - start_time).total_seconds()
            }
        
        # 중복 작업 체크 (uniq 기준)
        if uniq in active_uniq_tasks:
            existing_key = active_uniq_tasks[uniq]["key"]
            return {
                "key": key,
                "uniq": uniq,
                "callback": callback,
                "state": "duplicate",
                "data": {
                    "existing_key": existing_key
                },
                "message": f"UNIQ {uniq}에 대해 이미 작업이 진행 중입니다. (KEY: {existing_key})",
                "processing_time_seconds": (datetime.now() - start_time).total_seconds()
            }
        
        # 번역(~3초) → 큐 삽입 → 디스패처 배정 순서로 백그라운드 실행
        # 이벤트 루프가 1개이므로 스레드에서 처리
        # 
        thread = threading.Thread(
            target=_deepseek_translate_and_enqueue, # 백그라운드에서 실행할 함수
            args=(key, uniq, callback, prompt, count, datetime.now()) # 그 함수에 넘길 인자
        )
        thread.daemon = True
        thread.start() # 스레드 시작하면서 즉시 응답
        
        # 즉시 응답 반환
        return {
            "key": key,
            "uniq": uniq,
            "callback": callback,
            "state": "accepted",
            "message": "이미지 생성 작업이 시작되었습니다. 완료 시 콜백 URL로 결과를 전송합니다.",
            "processing_time_seconds": (datetime.now() - start_time).total_seconds()
        }
        
    except Exception as e:
        Logger.log(f"ERROR: Request processing failed: {e}")
        error_trace = traceback.format_exc()
        Logger.log(f"Detailed error:\n{error_trace}")
        return {
            "key": key if 'key' in locals() else "unknown",
            "uniq": uniq if 'uniq' in locals() else "unknown",
            "callback": callback if 'callback' in locals() else "unknown",
            "state": "error",
            "error": {
                "code": "INTERNAL_ERROR",
                "message": str(e),
                "traceback": error_trace
            },
            "message": "서버 내부 오류",
            "processing_time_seconds": (datetime.now() - start_time).total_seconds()
        }

@app.post("/perchance/generator")
async def async_generate_perchance_image(
    key: str = Form(..., description="작업 고유 번호"),
    uniq: str = Form(..., description="회원 디비 색인번호"),
    callback: str = Form(..., description="콜백 URL"),
    prompt: str = Form(..., description="이미지 생성 영어 프롬프트"),
    count: int = Form(default=4, description="생성할 이미지 수"),
    shape: str = Form(default="portrait", description="이미지 비율 (portrait/square/landscape)"),
):
    """Perchance 이미지 생성 API — 영어 프롬프트를 받아 폰보드로 이미지 생성 후 콜백 전송"""
    start_time = datetime.now()

    if not key or not uniq or not callback or not prompt:
        return {"key": key, "uniq": uniq, "state": "error", "message": "필수 파라미터 누락"}

    if not perchance_generator:
        return {"key": key, "uniq": uniq, "state": "error", "message": "Perchance 서비스가 초기화되지 않았습니다."}

    if uniq in active_uniq_tasks:
        existing_key = active_uniq_tasks[uniq]["key"]
        return {
            "key": key, "uniq": uniq, "state": "duplicate",
            "data": {"existing_key": existing_key},
            "message": f"UNIQ {uniq}에 대해 이미 작업이 진행 중입니다. (KEY: {existing_key})",
            "processing_time_seconds": (datetime.now() - start_time).total_seconds(),
        }

    enqueue_task({
        "task_type": "perchance",
        "key": key, "uniq": uniq, "callback": callback,
        "prompt": prompt, "count": count, "shape": shape,
        "queued_at": datetime.now(),
    })

    return {
        "key": key, "uniq": uniq, "callback": callback,
        "state": "accepted",
        "message": "Perchance 이미지 생성 작업이 시작되었습니다. 완료 시 콜백 URL로 결과를 전송합니다.",
        "processing_time_seconds": (datetime.now() - start_time).total_seconds(),
    }


@app.post("/cancel")
async def cancel_task(
    key: str = Form(..., description="취소할 작업의 고유 번호"),
    uniq: str = Form(..., description="회원 디비 색인번호")
):
    """작업 취소 API - 해당 key 및 uniq 할당된 디바이스의 동작을 멈추고 뒤로가기 실행"""
    try:
        # key로 작업 찾기
        task_found = False
        device_id = None
        
        if key in active_tasks:
            task_info = active_tasks[key]
            device_id = task_info.get("device_id")
            task_found = True
            
            # 중복 취소 요청 방지: 이미 취소된 작업이면 무시
            if task_info.get("cancelled") or task_info.get("status") == "cancelled":
                Logger.log(f"[CANCEL] 이미 취소된 작업입니다 (KEY={key}, UNIQ={uniq}, DEVICE={device_id})")
                return {
                    "key": key,
                    "uniq": uniq,
                    "device_id": device_id,
                    "state": "already_cancelled",
                    "message": "이미 취소된 작업입니다."
                }

            # 큐 대기 중 취소 — ADB 불필요, 디스패처가 스킵
            if task_info.get("status") == "queued":
                active_tasks[key]["cancelled"] = True
                active_tasks[key]["status"] = "cancelled"
                if uniq in active_uniq_tasks:
                    del active_uniq_tasks[uniq]
                Logger.log(f"[CANCEL] 대기 중 취소: KEY={key}, UNIQ={uniq}")
                callback_url = task_info.get("callback")
                if callback_url:
                    send_callback(callback_url, {
                        "key": key, "uniq": uniq, "callback": callback_url,
                        "state": "cancelled",
                        "data": {"translation": {"original": task_info.get("prompt", ""), "translated": None}},
                        "error": {"code": "TASK_CANCELLED", "message": "대기 중 취소되었습니다."},
                        "message": "대기 중 취소되었습니다.",
                        "processing_time_seconds": 0,
                    })
                return {
                    "key": key, "uniq": uniq, "device_id": None,
                    "state": "cancelled", "message": "대기 중 취소되었습니다."
                }

            # 취소 플래그 설정
            active_tasks[key]["cancelled"] = True
            active_tasks[key]["status"] = "cancelled"

            # multi_device_manager에 취소 표시
            if multi_device_manager:
                multi_device_manager.mark_as_cancelled(key)

            Logger.log(f"[CANCEL] KEY={key}, UNIQ={uniq}, DEVICE={device_id}")

            # 디바이스에 뒤로가기 명령 전송 (input keyevent 4) - 한 번만 실행
            if device_id and laixi_controller:
                try:
                    Logger.log(f"[{device_id}] 뒤로가기 명령 전송 (작업 취소)")
                    laixi_controller.execute_adb_command(device_id, "input keyevent 4")
                    time.sleep(0.5)
                    
                    # 디바이스 상태를 idle로 변경
                    if device_id in multi_device_manager.device_status:
                        multi_device_manager.device_status[device_id]['status'] = 'idle'
                        multi_device_manager.device_status[device_id]['current_task'] = None
                        Logger.log(f"[{device_id}] 디바이스 상태를 idle로 변경")
                    
                except Exception as device_error:
                    Logger.log(f"[{device_id}] 뒤로가기 명령 실패: {device_error}")
            
            # uniq 작업 삭제
            if uniq in active_uniq_tasks:
                del active_uniq_tasks[uniq]
            
            # 취소 콜백 전송 (exmaple2.py 형식)
            task_info = active_tasks.get(key, {})
            callback_url = task_info.get("callback")
            prompt_text = task_info.get("prompt", "")
            
            if callback_url:
                cancel_callback_data = {
                    "key": key,
                    "uniq": uniq,
                    "callback": callback_url,
                    "state": "cancelled",
                    "data": {
                        "translation": {
                            "original": prompt_text,
                            "translated": None
                        }
                    },
                    "error": {
                        "code": "TASK_CANCELLED",
                        "message": "사용자 요청으로 작업이 취소되었습니다.",
                        "details": f"Device: {device_id}"
                    },
                    "message": "작업이 취소되었습니다.",
                    "processing_time_seconds": 0
                }
                send_callback(callback_url, cancel_callback_data)
            
            return {
                "key": key,
                "uniq": uniq,
                "device_id": device_id,
                "state": "cancelled",
                "message": "작업이 취소되었습니다. 디바이스에 뒤로가기 명령을 전송했습니다."
            }
        
        # uniq로 작업 찾기 (key가 없을 경우)
        elif uniq in active_uniq_tasks:
            uniq_task_info = active_uniq_tasks[uniq]
            found_key = uniq_task_info.get("key")
            device_id = uniq_task_info.get("device_id")
            
            # 중복 취소 요청 방지: 이미 취소된 작업이면 무시
            if found_key in active_tasks:
                task_info = active_tasks[found_key]
                if task_info.get("cancelled") or task_info.get("status") == "cancelled":
                    Logger.log(f"[CANCEL] 이미 취소된 작업입니다 (UNIQ={uniq}, KEY={found_key}, DEVICE={device_id})")
                    return {
                        "key": found_key,
                        "uniq": uniq,
                        "device_id": device_id,
                        "state": "already_cancelled",
                        "message": "이미 취소된 작업입니다."
                    }
            
            Logger.log(f"[CANCEL] UNIQ={uniq}로 작업 찾음 (KEY={found_key}, DEVICE={device_id})")
            
            # active_tasks에서 취소 플래그 설정
            if found_key in active_tasks:
                active_tasks[found_key]["cancelled"] = True
                active_tasks[found_key]["status"] = "cancelled"
            
            # multi_device_manager에 취소 표시
            if multi_device_manager and found_key:
                multi_device_manager.mark_as_cancelled(found_key)
            
            # 디바이스에 뒤로가기 명령 전송 - 한 번만 실행
            if device_id and laixi_controller:
                try:
                    Logger.log(f"[{device_id}] 뒤로가기 명령 전송 (작업 취소)")
                    laixi_controller.execute_adb_command(device_id, "input keyevent 4")
                    time.sleep(0.5)
                    
                    # 디바이스 상태를 idle로 변경
                    if device_id in multi_device_manager.device_status:
                        multi_device_manager.device_status[device_id]['status'] = 'idle'
                        multi_device_manager.device_status[device_id]['current_task'] = None
                        Logger.log(f"[{device_id}] 디바이스 상태를 idle로 변경")
                    
                except Exception as device_error:
                    Logger.log(f"[{device_id}] 뒤로가기 명령 실패: {device_error}")
            
            # uniq 작업 삭제
            del active_uniq_tasks[uniq]
            
            # 취소 콜백 전송 (exmaple2.py 형식)
            if found_key in active_tasks:
                task_info = active_tasks.get(found_key, {})
                callback_url = task_info.get("callback")
                prompt_text = task_info.get("prompt", "")
                
                if callback_url:
                    cancel_callback_data = {
                        "key": found_key,
                        "uniq": uniq,
                        "callback": callback_url,
                        "state": "cancelled",
                        "data": {
                            "translation": {
                                "original": prompt_text,
                                "translated": None
                            }
                        },
                        "error": {
                            "code": "TASK_CANCELLED",
                            "message": "사용자 요청으로 작업이 취소되었습니다.",
                            "details": f"Device: {device_id}"
                        },
                        "message": "작업이 취소되었습니다.",
                        "processing_time_seconds": 0
                    }
                    send_callback(callback_url, cancel_callback_data)
            
            return {
                "key": found_key,
                "uniq": uniq,
                "device_id": device_id,
                "state": "cancelled",
                "message": "작업이 취소되었습니다. 디바이스에 뒤로가기 명령을 전송했습니다."
            }
        
        else:
            return {
                "key": key,
                "uniq": uniq,
                "state": "not_found",
                "message": "해당 KEY 또는 UNIQ에 대한 진행 중인 작업을 찾을 수 없습니다."
            }
            
    except Exception as e:
        Logger.log(f"[CANCEL] 취소 실패: {e}")
        traceback.print_exc()
        return {
            "key": key,
            "uniq": uniq,
            "state": "error",
            "error": {
                "code": "CANCEL_FAILED",
                "message": str(e)
            }
        }

@app.get("/devices")
async def get_devices():
    """디바이스 상태 조회"""
    if not multi_device_manager:
        raise HTTPException(status_code=503, detail="Multi-Device Manager가 초기화되지 않았습니다.")
    
    return {
        "devices": multi_device_manager.get_device_status(),
        "timestamp": datetime.now().isoformat()
    }

@app.get("/devices/status")
async def get_device_statistics():
    """디바이스 통계 정보"""
    if not multi_device_manager:
        raise HTTPException(status_code=503, detail="Multi-Device Manager가 초기화되지 않았습니다.")
    
    return multi_device_manager.get_device_statistics()

@app.post("/devices/stop-all")
async def stop_all_devices():
    """모든 작업 중단"""
    if not multi_device_manager:
        raise HTTPException(status_code=503, detail="Multi-Device Manager가 초기화되지 않았습니다.")
    
    multi_device_manager.stop_all_tasks()
    return {"message": "모든 작업이 중단되었습니다."}

if __name__ == "__main__":
    import uvicorn
    Logger.log("Multi-Device Grok Image Generation Server Start")
    uvicorn.run(app, host="127.0.0.1", port=8000)
