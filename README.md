# LangBot 学习计划

按课表安排次日计划、检测时间冲突，并发送课程和个人计划的私聊提醒。本仓库包含学习计划插件和可选的「约定提醒」桥接。

## 安装

1. 在 LangBot 插件管理上传 Release 中的 `local-study-planner-0.2.8.lbpkg`。
2. 配置目标私聊账号、机器人、提醒模型，以及机器人当前选中且绑定了本插件的流水线 UUID。
3. 复制 `timetable.json` 到插件运行时可访问的目录，填入自己的日期、时段和课程，再填写 `timetable_path`。本版从 `week7Monday` 起运行 70 天，时段格式为 `HH:MM-HH:MM`。内置课表日期固定，仅供示例。
4. 启用插件，在目标私聊发送一条消息，再用 `!规划` 检查。用 `!规划 18:00-19:00 复习` 提交次日计划，`!规划回应` 确认提醒，或用 `!暂停催促` 暂停催促。

需要日程联动时，再安装 `lin-rog-appointment-reminder-0.1.5.lbpkg`。两个插件绑定同一私聊和机器人，开启学习桥接，并共用 SQLite 文件。默认桥接路径为 `/app/data/plugins/.study_bridge/state.sqlite3`。只使用课程和手动计划时，可以关闭桥接。

## 验证

```sh
python -m pip install langbot-plugin==0.5.5 tzdata
python -m unittest discover -s tests
cd appointment-reminder-study-bridge
python -m unittest discover -s tests
```

测试使用合成课表和模拟消息发送。

## 0.2.8 更新

- 每天课表合成一条纯文字摘要，在首课前 30 至 60 分钟安排。重启沿用原发送时间，过窗后跳过；个人计划仍各自按时提醒。
- 配套约定提醒 0.1.5 将单次学习任务限定在计划当天，保留历史，跨过午夜时跳过未发送的过期任务。
