import threading
from abc import ABCMeta
from typing import Optional, Type, Dict, Union

import inject
import paho.mqtt.client as mqtt
import rospy
import yaml
import subprocess
import os
import signal
import atexit
import time
import json
import glob

from .util import lookup_object, extract_values, populate_instance

pid = []
labels = []
stop = False


def f():
    global pid
    if len(pid) > 0:
        for p in pid:
            os.killpg(p, signal.SIGTERM)
        pid = []
        rospy.loginfo("stoped!")
    else:
        rospy.loginfo("no stop!")


atexit.register(f)


def create_bridge(factory: Union[str, "Bridge"], msg_type: Union[str, Type[rospy.Message]], topic_from: str,
                  topic_to: str, frequency: Optional[float] = None, **kwargs) -> "Bridge":
    """ generate bridge instance using factory callable and arguments. if `factory` or `meg_type` is provided as string,
     this function will convert it to a corresponding object.
    """
    if isinstance(factory, str):
        factory = lookup_object(factory)
    if not issubclass(factory, Bridge):
        raise ValueError("factory should be Bridge subclass")
    if isinstance(msg_type, str):
        msg_type = lookup_object(msg_type)
    if not issubclass(msg_type, rospy.Message):
        raise TypeError(
            "msg_type should be rospy.Message instance or its string"
            "reprensentation")
    return factory(
        topic_from=topic_from, topic_to=topic_to, msg_type=msg_type, frequency=frequency, **kwargs)


class Bridge(object, metaclass=ABCMeta):
    """ Bridge base class """
    _mqtt_client = inject.attr(mqtt.Client)
    _serialize = inject.attr('serializer')
    _deserialize = inject.attr('deserializer')
    _extract_private_path = inject.attr('mqtt_private_path_extractor')


class RosToMqttBridge(Bridge):
    """ Bridge from ROS topic to MQTT

    bridge ROS messages on `topic_from` to MQTT topic `topic_to`. expect `msg_type` ROS message type.
    """

    def __init__(self, topic_from: str, topic_to: str, msg_type: rospy.Message, frequency: Optional[float] = None):
        self._topic_from = topic_from
        self._topic_to = self._extract_private_path(topic_to)
        self._last_published = rospy.get_time()
        self._interval = 0 if frequency is None else 1.0 / frequency
        rospy.Subscriber(topic_from, msg_type, self._callback_ros)

    def _callback_ros(self, msg: rospy.Message):
        # rospy.loginfo("ROS received from {}".format(self._topic_from))
        if (self._topic_from == '/tag_detections'):
            # rospy.loginfo(msg.detections[0].id[0])
            # rospy.loginfo(len(msg.detections))
            if (len(msg.detections) == 0):
                # rospy.loginfo("MQTT: No DATA to {}".format(self._topic_to))
                return
        now = rospy.get_time()
        if now - self._last_published >= self._interval:
            self._publish(msg)
            self._last_published = now

    def _publish(self, msg: rospy.Message):
        global labels, stop, pid

        try:
            if stop:
                stop = False
                if len(pid) > 0:
                    for p in pid:
                        os.killpg(p, signal.SIGTERM)
                    pid = []
                    rospy.loginfo("stoped!")

            # rospy.loginfo("MQTT send from {}".format(self._topic_to))
            # rospy.loginfo(msg.detections)
            if (self._topic_from == '/tag_detections' and self._topic_to == 'apriltagContent'):
                # self._serialize(msg.detections[0].id[0])  # extract_values(msg))
                payload = ",".join(['%s' % (d.id[0]) for d in msg.detections])
                #             payload = "[{}]".format(payload)
            elif (self._topic_from == '/tag_detections' and self._topic_to == 'apriltagSize'):
                # self._serialize(msg.detections[0].id[0])  # extract_values(msg))
                payload_json = [
                    {
                        'id': d.id[0],
                        'position': {
                            'x': d.pose.pose.pose.position.x,
                            'y': d.pose.pose.pose.position.y,
                            'z': d.pose.pose.pose.position.z
                        },
                        'orientation': {
                            'x': d.pose.pose.pose.orientation.x,
                            'y': d.pose.pose.pose.orientation.y,
                            'z': d.pose.pose.pose.orientation.z,
                            'w': d.pose.pose.pose.orientation.w
                        }
                    }
                    for d in msg.detections
                ]
                payload = json.dumps(payload_json)
                #             payload = "[{}]".format(payload)
            elif (self._topic_from == '/detectnet/detections' and self._topic_to == 'detectnetContent'):
                payload = ",".join(['%s:%.2f' % (d.Class, d.probability)
                                    for d in msg.bounding_boxes])
            elif (self._topic_from == '/detectnet/detections' and self._topic_to == 'detectnetSize'):
                # print(msg.detections)
                payload_json = [
                    {
                        'id': d.results[0].id,
                        'label': labels[d.results[0].id],
                        'score': d.results[0].score,
                        'bbox': {
                            'center': {
                                'x': d.bbox.center.x,
                                'y': d.bbox.center.y,
                                'theta': d.bbox.center.theta
                            },
                            'size_x': d.bbox.size_x,
                            'size_y': d.bbox.size_y
                        }
                    }
                    for d in msg.detections
                ]
                payload = json.dumps(payload_json)
            elif (self._topic_from == '/detectnet/vision_info' and self._topic_to == 'vision_info'):
                # Define the directory
                dir_path = msg.method

                # Get the .txt files in the directory
                txt_files = glob.glob(os.path.join(
                    os.path.dirname(dir_path), '*.txt'))

                # For each .txt file
                for txt_file in txt_files:
                    # Open the file
                    with open(txt_file, 'r') as f:
                        # Read the lines and convert each line into an element of a list
                        lines = [line.strip() for line in f]
                        labels = lines
                payload = json.dumps(labels)
            else:
                payload = yaml.dump(msg)
            self._mqtt_client.publish(topic=self._topic_to, payload=payload)
        except Exception as e:
            pass


class MqttToRosBridge(Bridge):
    """ Bridge from MQTT to ROS topic

    bridge MQTT messages on `topic_from` to ROS topic `topic_to`. MQTT messages will be converted to `msg_type`.
    """

    def __init__(self, topic_from: str, topic_to: str, msg_type: Type[rospy.Message],
                 frequency: Optional[float] = None, queue_size: int = 10):
        self._topic_from = self._extract_private_path(topic_from)
        self._topic_to = topic_to
        self._msg_type = msg_type
        self._queue_size = queue_size
        self._last_published = rospy.get_time()
        self._interval = None if frequency is None else 1.0 / frequency
        # Adding the correct topic to subscribe to
        self._mqtt_client.subscribe(self._topic_from)
        self._mqtt_client.message_callback_add(
            self._topic_from, self._callback_mqtt)

        if self._topic_to != "":
            self._publisher = rospy.Publisher(
                self._topic_to, self._msg_type, queue_size=self._queue_size)

    def run_subprocess(self, cmd):
        global pid

        proc = subprocess.Popen(
            cmd, shell=True, executable="/bin/bash", preexec_fn=os.setsid)
        rospy.loginfo("started!")
        rospy.loginfo(proc)
        pid.append(proc.pid)

    def _callback_mqtt(self, client: mqtt.Client, userdata: Dict, mqtt_msg: mqtt.MQTTMessage):
        """ callback from MQTT """
        # rospy.loginfo("MQTT received from {}".format(mqtt_msg.topic))
        # rospy.loginfo(mqtt_msg.payload)
        now = rospy.get_time()
        global pid, stop

        try:
            msg = mqtt_msg.payload.decode('UTF-8').split("|")
            # rospy.loginfo(mqtt_msg.payload.decode('UTF-8').split("|"))

            if msg[0] == 'start':
                cmd = []
                msgs = []

                for item in msg:
                    split_item = item.split('=')
                    if len(split_item) > 1:
                        msgs.append(split_item)

                if msg[1] == 'detectnet':
                    cmd = ["cd /home/nvidia/ros_workspace/devel && source setup.bash && roslaunch ros_deep_learning detectnet.ros1.launch" +
                           ' ' + ' '.join(['%s' % ':='.join(item) for item in msgs])]
                elif msg[1] == 'imagenet':
                    cmd = ["cd /home/nvidia/ros_workspace/devel && source setup.bash && roslaunch ros_deep_learning imagenet.ros1.launch" +
                           ' ' + ' '.join(['%s' % ':='.join(item) for item in msgs])]
                elif msg[1] == 'videoviewer':
                    cmd = ["cd /home/nvidia/ros_workspace/devel && source setup.bash && roslaunch ros_deep_learning video_viewer.ros1.launch" +
                           ' ' + ' '.join(['%s' % ':='.join(item) for item in msgs])]
                elif msg[1] == 'apriltag':
                    cmd = ["cd /home/nvidia/usb_cam_ws/devel && source setup.bash && roslaunch usb_cam usb_cam-test.launch", "sleep:5",
                           "cd /home/nvidia/apriltag_ws/devel_isolated && source setup.bash && roslaunch apriltag_ros continuous_detection.launch"]
                # rospy.loginfo(cmd)
                # , preexec_fn=os.setsid)
                if len(cmd) > 0:
                    for c in cmd:
                        cs = c.split(":")
                        if cs[0] == 'sleep':
                            time.sleep(int(cs[1]))
                        elif cs[0] != 'sleep':
                            thread = threading.Thread(
                                target=self.run_subprocess, args=(c,))
                            thread.start()

            if msg[0] == 'stop':
                stop = True
                try:
                    if len(pid) > 0:
                        for p in pid:
                            os.killpg(p, signal.SIGTERM)
                        pid = []
                        rospy.loginfo("stoped!")
                    else:
                        rospy.loginfo("no stop!")
                except:
                    rospy.loginfo("no ros to stop...")
                stop = False

            if self._topic_to != "":
                if self._interval is None or now - self._last_published >= self._interval:
                    try:
                        ros_msg = self._create_ros_message(mqtt_msg)
                        self._publisher.publish(ros_msg)
                        self._last_published = now
                    except Exception as e:
                        rospy.logerr(e)
        except Exception as e:
            rospy.logerr(e)

    def _create_ros_message(self, mqtt_msg: mqtt.MQTTMessage) -> rospy.Message:
        """ create ROS message from MQTT payload """
        # Hack to enable both, messagepack and json deserialization.
        if self._serialize.__name__ == "packb":
            msg_dict = self._deserialize(mqtt_msg.payload, raw=False)
        else:
            msg_dict = self._deserialize(mqtt_msg.payload)
        return populate_instance(msg_dict, self._msg_type())


__all__ = ['create_bridge', 'Bridge', 'RosToMqttBridge', 'MqttToRosBridge']
