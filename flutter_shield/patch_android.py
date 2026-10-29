import glob
import os
import re

manifest_path = "/tmp/shield_build/android/app/src/main/AndroidManifest.xml"
with open(manifest_path, "r", encoding="utf-8") as f:
    s = f.read()

perms = """<uses-permission android:name="android.permission.INTERNET"/>
    <uses-permission android:name="android.permission.WAKE_LOCK"/>
    <uses-permission android:name="android.permission.ACCESS_WIFI_STATE"/>
    <uses-permission android:name="android.permission.CHANGE_WIFI_STATE"/>
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_SPECIAL_USE"/>
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS"/>
    <uses-permission android:name="android.permission.VIBRATE"/>
    <uses-permission android:name="android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS"/>"""

if "android.permission.FOREGROUND_SERVICE_SPECIAL_USE" not in s:
    s = s.replace("<application", perms + '\n    <application android:allowBackup="false"')

service_decl = """        <service
            android:name=".ShieldForegroundService"
            android:enabled="true"
            android:exported="false"
            android:foregroundServiceType="specialUse">
            <property
                android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
                android:value="autonomous_market_monitoring_and_risk_defense"/>
        </service>
    </application>"""

if "ShieldForegroundService" not in s:
    s = s.replace("</application>", service_decl)

with open(manifest_path, "w", encoding="utf-8") as f:
    f.write(s)

for gf in glob.glob("/tmp/shield_build/android/**/*.gradle*", recursive=True):
    with open(gf, "r", encoding="utf-8") as f:
        txt = f.read()
    txt = re.sub(
        r'(id\s+["\']org\.jetbrains\.kotlin\.android["\']\s+version\s+["\'])[0-9.]+(["\'])',
        r'\g<1>1.9.24\2',
        txt,
    )
    txt = re.sub(
        r'(ext\.kotlin_version\s*=\s*["\'])[0-9.]+(["\'])',
        r'\g<1>1.9.24\2',
        txt,
    )
    with open(gf, "w", encoding="utf-8") as f:
        f.write(txt)

kt_files = glob.glob("/tmp/shield_build/android/app/src/main/kotlin/**/MainActivity.kt", recursive=True)
if kt_files:
    kt_code = """package com.projectshield.trader.shield

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.net.wifi.WifiManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.WindowManager
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class ShieldForegroundService : Service() {
    companion object {
        const val BG_CHANNEL_ID = "shield_bg_service"
        const val BG_NOTIF_ID = 1
    }

    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var silentTrack: AudioTrack? = null

    override fun onCreate() {
        super.onCreate()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                BG_CHANNEL_ID,
                "Shield Background Engine",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Keeps Project Shield autonomous monitoring alive in background"
                setShowBadge(false)
            }
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.createNotificationChannel(channel)
        }
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Shield::ForegroundServiceWakeLock").apply {
            setReferenceCounted(false)
            acquire()
        }
        try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val lockMode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                WifiManager.WIFI_MODE_FULL_LOW_LATENCY
            } else {
                @Suppress("DEPRECATION")
                WifiManager.WIFI_MODE_FULL_HIGH_PERF
            }
                        wifiLock = wm.createWifiLock(lockMode, "Shield::WifiWakeLock").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (_: Exception) {}
        try {
            val sampleRate = 8000
            val bufSize = AudioTrack.getMinBufferSize(
                sampleRate,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            ).coerceAtLeast(sampleRate * 2)
            val attrs = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .build()
            val fmt = AudioFormat.Builder()
                .setSampleRate(sampleRate)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .build()
            silentTrack = AudioTrack(attrs, fmt, bufSize, AudioTrack.MODE_STATIC, 0).apply {
                val silence = ByteArray(bufSize)
                write(silence, 0, silence.size)
                setLoopPoints(0, bufSize / 2, -1)
                setVolume(0.0f)
                play()
            }
        } catch (_: Exception) {}
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val launchIntent = Intent(this, MainActivity::class.java).apply {
            this.flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingFlags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        } else {
            PendingIntent.FLAG_UPDATE_CURRENT
        }
        val pendingIntent = PendingIntent.getActivity(this, 0, launchIntent, pendingFlags)

        val notification = NotificationCompat.Builder(this, BG_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_lock_idle_lock)
            .setContentTitle("🛡️ Project Shield Active")
            .setContentText("Autonomous market sentinel & risk defense running in background")
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(pendingIntent)
            .build()

        try {
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(BG_NOTIF_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            } else {
                startForeground(BG_NOTIF_ID, notification)
            }
        } catch (_: Exception) {
            try {
                startForeground(BG_NOTIF_ID, notification)
            } catch (_: Exception) {}
        }
        return START_STICKY
    }

    override fun onDestroy() {
        try {
            try { silentTrack?.stop(); silentTrack?.release() } catch (_: Exception) {}
            if (wakeLock?.isHeld == true) wakeLock?.release()
            if (wifiLock?.isHeld == true) wifiLock?.release()
        } catch (_: Exception) {}
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}

class MainActivity: FlutterActivity() {
    private val CHANNEL = "shield/native"
    private val ALERT_CHANNEL_ID = "shield_high_alerts"
    private var notifId = 100
    private var methodChannel: MethodChannel? = null
    private val bgHandler = Handler(Looper.getMainLooper())
        private val bgRunnable = object : Runnable {
        override fun run() {
            try {
                flutterEngine?.lifecycleChannel?.appIsResumed()
                methodChannel?.invokeMethod("nativeTick", null)
            } catch (_: Exception) {}
            bgHandler.postDelayed(this, 12000L)
        }
    }

    override fun onPause() {
        super.onPause()
        try { flutterEngine?.lifecycleChannel?.appIsResumed() } catch (_: Exception) {}
    }

    override fun onStop() {
        super.onStop()
        try { flutterEngine?.lifecycleChannel?.appIsResumed() } catch (_: Exception) {}
    }

    override fun onUserLeaveHint() {
        super.onUserLeaveHint()
        try { flutterEngine?.lifecycleChannel?.appIsResumed() } catch (_: Exception) {}
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        createAlertNotificationChannel()
        requestNotificationPermission()
        startShieldForegroundService()
    }

    override fun onDestroy() {
        bgHandler.removeCallbacks(bgRunnable)
        super.onDestroy()
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        val ch = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
        methodChannel = ch
        bgHandler.removeCallbacks(bgRunnable)
        bgHandler.postDelayed(bgRunnable, 25000L)
        ch.setMethodCallHandler { call, result ->
            when (call.method) {
                "notify" -> {
                    val title = call.argument<String>("title") ?: "Project Shield Alert"
                    val body = call.argument<String>("body") ?: ""
                    showNotification(title, body)
                    result.success(true)
                }
                "startBackground" -> {
                    startShieldForegroundService()
                    requestIgnoreBatteryOptimizations()
                    result.success(true)
                }
                else -> result.notImplemented()
            }
        }
    }

    private fun startShieldForegroundService() {
        try {
            val serviceIntent = Intent(this, ShieldForegroundService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(serviceIntent)
            } else {
                startService(serviceIntent)
            }
        } catch (_: Exception) {}
    }

    private fun createAlertNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                ALERT_CHANNEL_ID,
                "Shield Autonomous Alerts",
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "High-priority alerts and autonomous trade notifications"
                enableVibration(true)
            }
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.createNotificationChannel(channel)
        }
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33) {
            if (ActivityCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 101)
            }
        }
    }

    private fun showNotification(title: String, body: String) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val builder = NotificationCompat.Builder(this, ALERT_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .setAutoCancel(true)
        nm.notify(notifId++, builder.build())
    }

    private fun requestIgnoreBatteryOptimizations() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            if (!pm.isIgnoringBatteryOptimizations(packageName)) {
                try {
                    val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                        data = Uri.parse("package:$packageName")
                    }
                    startActivity(intent)
                } catch (_: Exception) {}
            }
        }
    }
}
"""
    with open(kt_files[0], "w", encoding="utf-8") as f:
        f.write(kt_code)
print("Successfully patched Android manifest, Gradle, and Kotlin ForegroundService.")
