package io.github.youxiangyu.keepsite.core

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Only opaque app cookies and short-lived login proofs; no provider tokens. */
class SecretStore(context: Context) {
    private val directory = File(context.noBackupFilesDir, "session-secrets").apply { mkdirs() }
    private val alias = "keepsite-session-v1"
    private fun file(name: String): AtomicFile {
        require(name.matches(Regex("[a-z-]{1,40}")))
        return AtomicFile(File(directory, name))
    }
    private fun key(create: Boolean): SecretKey? {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        if (!create) return null
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true).build())
        }.generateKey()
    }
    @Synchronized fun read(name: String): String? {
        return try {
            val raw = file(name).openRead().use { stream ->
                val bytes = stream.readBytes()
                if (bytes.size !in 29..16384) return null
                bytes
            }
            val secret = key(false) ?: return null
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, secret, GCMParameterSpec(128, raw.copyOfRange(0, 12)))
            cipher.updateAAD(name.toByteArray())
            cipher.doFinal(raw.copyOfRange(12, raw.size)).toString(Charsets.UTF_8)
        } catch (_: Exception) { null }
    }
    @Synchronized fun write(name: String, value: String) {
        require(value.toByteArray().size <= 12000)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key(true))
        cipher.updateAAD(name.toByteArray())
        val target = file(name)
        val output = target.startWrite()
        try {
            output.write(cipher.iv)
            output.write(cipher.doFinal(value.toByteArray(Charsets.UTF_8)))
            target.finishWrite(output)
        } catch (failure: Exception) { target.failWrite(output); throw failure }
    }
    @Synchronized fun clear(name: String) { file(name).delete() }
}
