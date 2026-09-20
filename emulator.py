"""Authenticated local Android Emulator screen and input transport."""
import os
from pathlib import Path
import re
import sys
import grpc
from google.protobuf.empty_pb2 import Empty
import emulator_pb2 as pb

SERVICE = '/android.emulation.control.EmulatorController/'


def discovery_folders():
    """Return platform discovery folders without exposing their token files."""
    override = os.environ.get('PLIPA_EMULATOR_DISCOVERY')
    if override:
        return [Path(override)]
    if sys.platform == 'win32':
        root = Path(os.environ['LOCALAPPDATA']) if os.environ.get('LOCALAPPDATA') else Path.home()
        return [root/'Temp/avd/running']
    if sys.platform == 'darwin':
        return [Path.home()/'Library/Caches/TemporaryItems/avd/running',
                Path.home()/'Library/Android/avd/running']
    folders = []
    if os.environ.get('XDG_RUNTIME_DIR'):
        folders.append(Path(os.environ['XDG_RUNTIME_DIR'])/'avd/running')
    for key in ('ANDROID_EMULATOR_HOME', 'ANDROID_PREFS_ROOT', 'ANDROID_SDK_HOME'):
        if os.environ.get(key):
            folders.append(Path(os.environ[key])/'avd/running')
    folders.append(Path.home()/'.android/avd/running')
    return folders


def stream_size():
    try:
        size = int(os.environ.get('PLIPA_STREAM_SIZE', '720'))
    except ValueError:
        size = 720
    return min(1920, max(240, size))


def screenshot_size(image):
    return image.format.width or image.width, image.format.height or image.height


class Connection:
    def __init__(self, serial):
        if not isinstance(serial, str) or not re.fullmatch(r'emulator-\d+', serial):
            raise ValueError('로컬 Android 에뮬레이터만 지원합니다.')
        settings = None
        for folder in discovery_folders():
            for path in folder.glob('pid_*.ini'):
                try:
                    candidate = dict(line.split('=', 1) for line in path.read_text(encoding='utf-8').splitlines() if '=' in line)
                    if candidate.get('port.serial') == serial.split('-')[1] and candidate.get('grpc.token'):
                        settings = candidate
                        break
                except OSError:
                    continue
            if settings:
                break
        if not settings or not settings.get('grpc.port', '').isdigit():
            raise ValueError('화면 연결 설정이 없습니다. 에뮬레이터를 종료하고 플리파에서 다시 시작하세요.')
        self.channel = grpc.insecure_channel('127.0.0.1:' + settings['grpc.port'],
                                             options=[('grpc.max_receive_message_length', 16777216)])
        self.auth = [('authorization', 'Bearer ' + settings['grpc.token'])]
        try:
            shot = self.call('getScreenshot', pb.ImageFormat(format=pb.ImageFormat.PNG), pb.Image)
            self.size = screenshot_size(shot)
            if not all(self.size):
                raise ValueError('에뮬레이터 화면 크기를 확인하지 못했습니다.')
        except Exception:
            self.close()
            raise

    def call(self, name, message, response=Empty):
        try:
            return self.channel.unary_unary(SERVICE + name,
                request_serializer=type(message).SerializeToString,
                response_deserializer=response.FromString)(message, metadata=self.auth, timeout=2)
        except grpc.RpcError:
            raise ValueError('에뮬레이터 연결이 끊겼습니다. 다시 연결하세요.') from None

    def frames(self):
        size = stream_size()
        return self.channel.unary_stream(SERVICE + 'streamScreenshot',
            request_serializer=pb.ImageFormat.SerializeToString,
            response_deserializer=pb.Image.FromString)(
                pb.ImageFormat(format=pb.ImageFormat.PNG, width=size, height=size), metadata=self.auth)

    def mouse(self, x, y, pressed):
        width, height = self.size
        self.call('sendMouse', pb.MouseEvent(x=round(x*(width-1)), y=round(y*(height-1)), buttons=int(pressed)))

    def key(self, key):
        self.call('sendKey', pb.KeyboardEvent(key=key, eventType=pb.KeyboardEvent.keypress))

    def close(self):
        self.channel.close()
