import org.jetbrains.kotlin.gradle.dsl.JvmTarget
plugins { id("com.android.application") version "9.3.3" }
android {
    namespace = "io.github.youxiangyu.keepsite"
    compileSdk = 37
    buildToolsVersion = "36.0.0"
    defaultConfig {
        applicationId = "io.github.youxiangyu.keepsite.networkverification"
        minSdk = 26; targetSdk = 36; versionCode = 3; versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("String", "API_BASE_URL", "\"https://keepsite.space\"")
    }
    sourceSets.getByName("main") {
        kotlin.directories += "../../main/java"
        res.directories += "../../main/res"
        assets.directories += "../../test/resources"
    }
    sourceSets.getByName("androidTest").kotlin.directories += "src/displayGuard/kotlin"
    sourceSets.getByName("test").kotlin.directories += "src/displayGuard/kotlin"
    signingConfigs.getByName("debug") { storeFile = file(providers.gradleProperty("verificationSigningStore").get()) }
    buildFeatures { buildConfig = true; viewBinding = true }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
}
kotlin { compilerOptions { jvmTarget = JvmTarget.JVM_17 } }
dependencies {
    testImplementation("junit:junit:4.13.2")
    implementation("androidx.core:core-ktx:1.19.0")
    implementation("androidx.activity:activity-ktx:1.13.0")
    implementation("androidx.appcompat:appcompat:1.8.0")
    implementation("com.google.android.material:material:1.14.0")
    implementation("androidx.recyclerview:recyclerview:1.2.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.6.2")
    implementation("androidx.lifecycle:lifecycle-viewmodel-ktx:2.6.2")
    implementation("com.squareup.okhttp3:okhttp:5.3.2")
    implementation("io.github.webrtc-sdk:android:150.7871.01")
    androidTestImplementation("androidx.test:runner:1.7.0")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.7.0")
}
