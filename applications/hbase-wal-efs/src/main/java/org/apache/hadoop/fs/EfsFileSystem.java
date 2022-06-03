/**
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
 * INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
 * PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
 * HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
 * OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
 * SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 */
package org.apache.hadoop.fs;

import org.apache.hadoop.conf.Configuration;
import org.apache.hadoop.fs.permission.FsPermission;
import org.apache.hadoop.io.IOUtils;
import org.apache.hadoop.io.nativeio.NativeIO;
import org.apache.hadoop.util.Progressable;
import org.apache.hadoop.util.Shell;
import org.apache.hadoop.util.StringUtils;
import org.apache.yetus.audience.InterfaceAudience;

import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.URI;
import java.util.EnumSet;

/****************************************************************
 * Implement the FileSystem API for the EFS filesystem.
 *
 *****************************************************************/
@InterfaceAudience.Private
public class EfsFileSystem extends RawLocalFileSystem{
  static final URI NAME = URI.create("efs:///");

  public EfsFileSystem() {
    super();
    LOG.info("Initialize EfsFileSystem.");
  }

  @Override
  public URI getUri() { return NAME; }

  @Override
  public void initialize(URI uri, Configuration conf) throws IOException {
    super.initialize(uri, conf);
    setConf(conf);
  }

  class EfsBufferedOutputStream extends BufferedOutputStream implements Syncable, StreamCapabilities {


    public EfsBufferedOutputStream(OutputStream out) {
      super(out);
    }

    public EfsBufferedOutputStream(OutputStream out, int size) {
      super(out, size);
    }

    @Override public boolean hasCapability(String capability) {
      if(out instanceof StreamCapabilities) {
        return ((StreamCapabilities)out).hasCapability(capability);
      }
      return false;
    }

    @Override public void hflush() throws IOException {
      flush();
      if(out instanceof Syncable) {
        ((Syncable)out).hflush();
      }
    }

    @Override public void hsync() throws IOException {
      flush();
      if(out instanceof Syncable) {
        ((Syncable)out).hsync();
      }
    }
  }
  /*********************************************************
   * For create()'s FSOutputStream.
   *********************************************************/
  class EfsFSFileOutputStream extends OutputStream implements Syncable, StreamCapabilities {
    public static final String WAL_EFS_HSYNC_CONF_KEY = "hbase.wal.efs.fdsync";
    public static final boolean DEFAULT_WAL_EFS_HSYNC = false;

    private FileOutputStream fos;

    private EfsFSFileOutputStream(Path f, boolean append,
                                  FsPermission permission) throws IOException {
      File file = pathToFile(f);
      if (permission == null) {
        this.fos = new FileOutputStream(file, append);
      } else {
        if (Shell.WINDOWS && NativeIO.isAvailable()) {
          this.fos = NativeIO.Windows.createFileOutputStreamWithMode(file,
              append, permission.toShort());
        } else {
          this.fos = new FileOutputStream(file, append);
          boolean success = false;
          try {
            setPermission(f, permission);
            success = true;
          } finally {
            if (!success) {
              IOUtils.cleanup(LOG, this.fos);
            }
          }
        }
      }
    }

    /*
     * Just forward to the fos
     */
    @Override
    public void close() throws IOException {
      fos.close();
    }
    @Override
    public void flush() throws IOException {
      fos.flush();
    }

    @Override
    public void hflush() throws IOException {
      fos.flush();
      if(getConf().getBoolean(WAL_EFS_HSYNC_CONF_KEY, DEFAULT_WAL_EFS_HSYNC)) {
        fos.getFD().sync();
      }
    }

    @Override
    public void hsync() throws IOException {
      fos.flush();
      fos.getFD().sync();
    }

    @Override
    public boolean hasCapability(String capability) {
      switch (StringUtils.toLowerCase(capability)) {
        case StreamCapabilities.HSYNC:
        case StreamCapabilities.HFLUSH:
          return true;
        default:
          return false;
      }
    }

    @Override
    public void write(byte[] b, int off, int len) throws IOException {
      try {
        fos.write(b, off, len);
      } catch (IOException e) {                // unexpected exception
        throw new FSError(e);                  // assume native fs error
      }
    }

    @Override
    public void write(int b) throws IOException {
      try {
        fos.write(b);
      } catch (IOException e) {              // unexpected exception
        throw new FSError(e);                // assume native fs error
      }
    }
  }

  @Override
  public FSDataOutputStream append(Path f, int bufferSize,
                                   Progressable progress) throws IOException {
    FileStatus status = getFileStatus(f);
    if (status.isDirectory()) {
      throw new IOException("Cannot append to a diretory (=" + f + " )");
    }
    return new FSDataOutputStream(new EfsBufferedOutputStream(
        createOutputStreamWithMode(f, true, null), bufferSize), statistics,
        status.getLen());
  }

  @Override
  public FSDataOutputStream create(Path f, FsPermission permission,
                                   boolean overwrite, int bufferSize, short replication, long blockSize,
                                   Progressable progress) throws IOException {

    FSDataOutputStream out = createEfs(f, overwrite, true, bufferSize, replication,
        blockSize, progress, permission);
    return out;
  }

  @Override
  public FSDataOutputStream create(Path f, boolean overwrite, int bufferSize,
                                   short replication, long blockSize, Progressable progress)
      throws IOException {
    return createEfs(f, overwrite, true, bufferSize, replication, blockSize,
        progress, null);
  }

  private FSDataOutputStream createEfs(Path f, boolean overwrite,
                                       boolean createParent, int bufferSize, short replication, long blockSize,
                                       Progressable progress, FsPermission permission) throws IOException {
    if (exists(f) && !overwrite) {
      throw new FileAlreadyExistsException("File already exists: " + f);
    }
    Path parent = f.getParent();
    if (parent != null && !mkdirs(parent)) {
      throw new IOException("Mkdirs failed to create " + parent.toString());
    }
    return new FSDataOutputStream(new EfsBufferedOutputStream(
        createOutputStreamWithMode(f, false, permission), bufferSize),
        statistics);
  }

  @Override
  public FSDataOutputStream createNonRecursive(Path f, FsPermission permission,
                                               boolean overwrite,
                                               int bufferSize, short replication, long blockSize,
                                               Progressable progress) throws IOException {
    FSDataOutputStream out = createEfs(f, overwrite, false, bufferSize, replication,
        blockSize, progress, permission);
    return out;
  }

  @Override
  public FSDataOutputStream createNonRecursive(Path f, FsPermission permission,
                                               EnumSet<CreateFlag> flags, int bufferSize, short replication, long blockSize,
                                               Progressable progress) throws IOException {
    if (exists(f) && !flags.contains(CreateFlag.OVERWRITE)) {
      throw new FileAlreadyExistsException("File already exists: " + f);
    }
    return new FSDataOutputStream(new EfsBufferedOutputStream(
        createOutputStreamWithMode(f, false, permission), bufferSize),
        statistics);
  }

  @Override
  protected OutputStream createOutputStreamWithMode(Path f, boolean append,
                                                    FsPermission permission) throws IOException {
    return new EfsFSFileOutputStream(f, append, permission);
  }

  @Override
  public String toString() {
    return "EfsFS";
  }

}

