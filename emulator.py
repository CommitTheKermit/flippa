"""Authenticated local Android Emulator screen and input transport."""
from pathlib import Path
import re
import grpc
from google.protobuf.empty_pb2 import Empty
import emulator_pb2 as pb

SERVICE = '/android.emulation.control.EmulatorController/'


class Connection:
    def __init__(self, serial):
        if not isinstance(serial, str) or not re.fullmatch(r'emulator-\d+', serial):
            raise ValueError('로컬 Android 에뮬레이터만 지원합니다.')
        settings = None
        folder = Path.home()/'Library/Caches/TemporaryItems/avd/running'
        for path in folder.glob('pid_*.ini'):
            try:
                candidate = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
                if candidate.get('port.serial') == serial.split('-')[1] and candidate.get('grpc.token'):
                    settings = candidate
                    break
            except OSError:
                continue
        if not settings or not settings.get('grpc.port', '').isdigit():
            raise ValueError('화면 연결 설정이 없습니다. 에뮬레이터를 종료하고 플리파에서 다시 시작하세요.')
        self.channel = grpc.insecure_channel('127.0.0.1:' + settings['grpc.port'],
                                             options=[('grpc.max_receive_message_length', 16777216)])
        self.auth = [('authorization', 'Bearer ' + settings['grpc.token'])]
        try:
            shot = self.call('getScreenshot', pb.ImageFormat(format=pb.ImageFormat.PNG), pb.Image)
            self.size = (shot.format.width, shot.format.height)
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
        return self.channel.unary_stream(SERVICE + 'streamScreenshot',
            request_serializer=pb.ImageFormat.SerializeToString,
            response_deserializer=pb.Image.FromString)(
                pb.ImageFormat(format=pb.ImageFormat.PNG, width=960, height=960), metadata=self.auth)

    def mouse(self, x, y, pressed):
        width, height = self.size
        self.call('sendMouse', pb.MouseEvent(x=round(x*(width-1)), y=round(y*(height-1)), buttons=int(pressed)))

    def key(self, key):
        self.call('sendKey', pb.KeyboardEvent(key=key, eventType=pb.KeyboardEvent.keypress))

    def close(self):
        self.channel.close()
