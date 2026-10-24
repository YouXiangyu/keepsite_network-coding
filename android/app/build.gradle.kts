import java.net.URI
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
}

val productionOrigin = "https://keepsite.space"
val debugOrigin = providers.gradleProperty("keepsite.debugBaseUrl").orElse(productionOrigin).get().trimEnd('/')
if (debugOrigin != productionOrigin) {
    val uri = URI(debugOrigin)
    require(uri.scheme == "http" && uri.host in setOf("127.0.0.1", "localhost", "::1", "[::1]") && uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null && uri.rawPath.isNullOrEmpty()) {
        "Debug API override must be an HTTP loopback origin"
    }
}

android {
    namespace = "io.github.youxiangyu.keepsite"
    compileSdk = 37
    buildToolsVersion = "36.0.0"
    defaultConfig {
        applicationId = "io.github.youxiangyu.keepsite"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("String", "API_BASE_URL", "\"$productionOrigin\"")
    }
    buildTypes {
        debug { buildConfigField("String", "API_BASE_URL", "\"$debugOrigin\"") }
        release { isMinifyEnabled = false }
    }
    signingConfigs.getByName("debug") {
        storeFile = rootProject.file("../.cache/android-signing/debug.keystore")
    }
    buildFeatures {
        buildConfig = true
        viewBinding = true
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    testOptions { unitTests.isReturnDefaultValues = false }
}

kotlin { compilerOptions { jvmTarget = JvmTarget.JVM_17 } }

dependencies {
    implementation("androidx.core:core-ktx:1.19.0")
    implementation("androidx.activity:activity-ktx:1.13.0")
    implementation("androidx.appcompat:appcompat:1.8.0")
    implementation("com.google.android.material:material:1.14.0")
    implementation("androidx.recyclerview:recyclerview:1.2.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.6.2")
    implementation("androidx.lifecycle:lifecycle-viewmodel-ktx:2.6.2")
    implementation("com.squareup.okhttp3:okhttp:5.3.2")
    implementation("io.github.webrtc-sdk:android:150.7871.01")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
    androidTestImplementation("androidx.test:runner:1.7.0")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.7.0")
}

val debugKey = rootProject.file("../.cache/android-signing/debug.keystore")
val prepareDebugSigning by tasks.registering(Exec::class) {
    description = "Create only the local debug identity in the workspace cache."
    outputs.file(debugKey)
    onlyIf { !debugKey.isFile }
    doFirst { debugKey.parentFile.mkdirs() }
    val executable = if (System.getProperty("os.name").startsWith("Windows")) "keytool.exe" else "keytool"
    commandLine(File(System.getProperty("java.home"), "bin/$executable"), "-genkeypair", "-keystore", debugKey,
        "-storepass", "android", "-alias", "androiddebugkey", "-keypass", "android", "-dname", "CN=Android Debug,O=Android,C=US",
        "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000", "-noprompt")
}
tasks.matching { it.name == "validateSigningDebug" }.configureEach { dependsOn(prepareDebugSigning) }

tasks.register("resolveDebugArtifacts") {
    group = "verification"
    description = "Resolve the declared Android runtime and local test artifacts without launching a device."
    doLast {
        listOf("debugRuntimeClasspath", "debugUnitTestRuntimeClasspath").forEach { name ->
            configurations.getByName(name).files.sortedBy { it.name }.forEach { println(it.absolutePath) }
        }
    }
}
